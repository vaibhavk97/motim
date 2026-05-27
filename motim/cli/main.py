"""Main CLI entry point for MOTIM."""

from pathlib import Path

import click

from motim.agent_replay import diff_exchanges, replay_exchange
from motim.config import get_config
from motim.exchange_db import ExchangeDB

from .config_cmd import config
from .proxy import proxy
from .proxy import start as proxy_start
from .proxy import status as proxy_status
from .proxy import stop as proxy_stop
from .proxy import trust_cert as proxy_trust_cert
from .services import services


@click.group()
@click.version_option(package_name="motim")
def cli():
    """MOTIM (Model Over Traffic — Intercept & Manage) - API traffic capture & replay for agents.

    Capture web API traffic and enable AI agents to make authenticated
    requests to any service.

    Quick start:
        motim init           # First-time setup
        motim proxy start    # Start capturing traffic
        motim services       # See captured services
    """
    pass


# Register command groups
cli.add_command(proxy)
cli.add_command(services)
cli.add_command(config)


def _open_db(db_path: str | None = None) -> ExchangeDB:
    cfg = get_config()
    path = db_path or cfg.capture.exchange_db_path
    return ExchangeDB(Path(path).expanduser(), max_body_bytes=cfg.capture.max_body_bytes)


def _get_header_value(headers_list: list[dict[str, str]], name: str) -> str | None:
    """Case-insensitive header lookup from get_exchange() headers format."""
    lower = name.lower()
    for h in headers_list:
        if h["name"].lower() == lower:
            return h["value"]
    return None


def _decode_body(
    body_bytes: bytes | None,
    content_type: str | None,
    *,
    raw: bool = False,
    content_encoding: str | None = None,
) -> str:
    """Decode body bytes to a display string.

    Handles gzip/deflate decompression and JSON pretty-printing.
    Returns ``<binary: N bytes>`` for non-text content.
    """
    import gzip
    import json as _json
    import zlib

    if body_bytes is None:
        return ""

    ct = (content_type or "").lower()
    is_text = any(
        t in ct
        for t in ("text/", "json", "xml", "html", "javascript", "ecmascript", "form-urlencoded")
    )
    if not is_text and not raw:
        return f"<binary: {len(body_bytes)} bytes>"

    # Decompress if needed
    data = body_bytes
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
            return f"<binary: {len(body_bytes)} bytes>"

    if raw:
        return text

    # Pretty-print JSON
    if "json" in ct:
        try:
            obj = _json.loads(text)
            return _json.dumps(obj, indent=2, ensure_ascii=False)
        except Exception:
            pass

    return text


@cli.command()
@click.argument("exchange_id", type=int)
@click.option("--request-only", is_flag=True, help="Show only the request")
@click.option("--response-only", is_flag=True, help="Show only the response")
@click.option("--raw", is_flag=True, help="Show raw body without pretty-printing")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
@click.option("--db", "db_path", help="Path to SQLite exchange DB (defaults to config)")
def show(
    exchange_id: int,
    request_only: bool,
    response_only: bool,
    raw: bool,
    as_json: bool,
    db_path: str | None,
):
    """Show a full captured exchange (request + response)."""
    import json as _json

    with _open_db(db_path) as db:
        ex = db.get_exchange(exchange_id)
        req_headers = ex["headers"]["request"]
        resp_headers = ex["headers"]["response"]
        req_body = ex["bodies"]["request"]
        resp_body = ex["bodies"]["response"]

        req_ct = ex.get("req_content_type") or _get_header_value(req_headers, "content-type") or ""
        resp_ct = (
            ex.get("resp_content_type") or _get_header_value(resp_headers, "content-type") or ""
        )
        req_enc = _get_header_value(req_headers, "content-encoding")
        resp_enc = _get_header_value(resp_headers, "content-encoding")

        def _body_or_missing(body, ct, enc, headers):
            if body is None or (isinstance(body, bytes) and len(body) == 0):
                cl = _get_header_value(headers, "content-length")
                return None, True, int(cl) if cl and cl.isdigit() else None
            return _decode_body(body, ct, raw=raw, content_encoding=enc), False, None

        if as_json:
            payload: dict[str, object] = {}
            if not response_only:
                req_decoded, req_missing, req_cl = _body_or_missing(
                    req_body, req_ct, req_enc, req_headers
                )
                req_obj: dict[str, object] = {
                    "method": ex.get("method"),
                    "url": ex.get("url"),
                    "headers": {h["name"]: h["value"] for h in req_headers},
                    "body": req_decoded,
                }
                if req_missing:
                    req_obj["body_missing"] = True
                    if req_cl:
                        req_obj["content_length"] = req_cl
                payload["request"] = req_obj
            if not request_only:
                resp_decoded, resp_missing, resp_cl = _body_or_missing(
                    resp_body, resp_ct, resp_enc, resp_headers
                )
                resp_obj: dict[str, object] = {
                    "status": ex.get("status"),
                    "headers": {h["name"]: h["value"] for h in resp_headers},
                    "body": resp_decoded,
                }
                if resp_missing:
                    resp_obj["body_missing"] = True
                    if resp_cl:
                        resp_obj["content_length"] = resp_cl
                payload["response"] = resp_obj
            click.echo(_json.dumps(payload, ensure_ascii=False))
        else:
            if not response_only:
                click.echo("=== Request ===")
                method = ex.get("method", "GET")
                path = ex.get("path", "/")
                query = ex.get("query")
                if query:
                    path = f"{path}?{query}"
                click.echo(f"{method} {path} HTTP/1.1")
                for h in req_headers:
                    click.echo(f"{h['name']}: {h['value']}")
                req_decoded, req_missing, req_cl = _body_or_missing(
                    req_body, req_ct, req_enc, req_headers
                )
                if req_missing:
                    hint = f" (content-length: {req_cl})" if req_cl else ""
                    click.echo(f"\n<body not captured{hint}>")
                elif req_decoded:
                    click.echo("")
                    click.echo(req_decoded)

            if not request_only:
                if not response_only:
                    click.echo("")
                click.echo("=== Response ===")
                status = ex.get("status", 0)
                click.echo(f"HTTP/1.1 {status}")
                for h in resp_headers:
                    click.echo(f"{h['name']}: {h['value']}")
                resp_decoded, resp_missing, resp_cl = _body_or_missing(
                    resp_body, resp_ct, resp_enc, resp_headers
                )
                if resp_missing:
                    hint = f" (content-length: {resp_cl})" if resp_cl else ""
                    click.echo(f"\n<body not captured{hint}>")
                elif resp_decoded:
                    click.echo("")
                    click.echo(resp_decoded)


@cli.command()
@click.argument("exchange_id", type=int)
@click.option(
    "--request", "use_request", is_flag=True, help="Dump request body instead of response"
)
@click.option("--raw", is_flag=True, help="Skip pretty-printing")
@click.option("--db", "db_path", help="Path to SQLite exchange DB (defaults to config)")
def cat(
    exchange_id: int,
    use_request: bool,
    raw: bool,
    db_path: str | None,
):
    """Dump only the body of an exchange (response by default).

    No headers, no framing — suitable for piping to jq, etc.
    """
    with _open_db(db_path) as db:
        ex = db.get_exchange(exchange_id)
        side = "request" if use_request else "response"
        body = ex["bodies"][side]
        headers = ex["headers"][side]
        if body is None or (isinstance(body, bytes) and len(body) == 0):
            cl = _get_header_value(headers, "content-length")
            hint = f" (content-length: {cl})" if cl else ""
            click.echo(
                f"No {side} body stored for exchange {exchange_id}{hint}. "
                "Body may have been streamed (> stream_large_bodies threshold).",
                err=True,
            )
            return
        ct_field = "req_content_type" if use_request else "resp_content_type"
        ct = ex.get(ct_field) or _get_header_value(headers, "content-type") or ""
        enc = _get_header_value(headers, "content-encoding")
        text = _decode_body(body, ct, raw=raw, content_encoding=enc)
        if text:
            click.echo(text)


@cli.command()
@click.argument("exchange_id", type=int)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["curl"], case_sensitive=False),
    default="curl",
    show_default=True,
    help="Export format",
)
@click.option("--db", "db_path", help="Path to SQLite exchange DB (defaults to config)")
def export(exchange_id: int, fmt: str, db_path: str | None):
    """Export a captured exchange as a runnable command.

    Currently supports curl format.
    """
    _HOP_BY_HOP = frozenset(
        {
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailers",
            "transfer-encoding",
            "upgrade",
            "host",
            "content-length",
        }
    )

    with _open_db(db_path) as db:
        ex = db.get_exchange(exchange_id)
        req_headers = ex["headers"]["request"]
        req_body = ex["bodies"]["request"]
        method = ex.get("method", "GET")
        url = ex.get("url", "")

        safe_method = method.replace("'", "'\\''")
        parts = [f"curl -X '{safe_method}'"]

        for h in req_headers:
            if h["name"].lower() in _HOP_BY_HOP:
                continue
            # Shell-safe: use single quotes, escaping embedded single quotes
            val = f"{h['name']}: {h['value']}"
            safe = val.replace("'", "'\\''")
            parts.append(f"  -H '{safe}'")

        if req_body:
            ct = ex.get("req_content_type") or _get_header_value(req_headers, "content-type") or ""
            body_text = _decode_body(req_body, ct, raw=True)
            if body_text:
                safe_body = body_text.replace("'", "'\\''")
                parts.append(f"  --data-raw '{safe_body}'")

        safe_url = url.replace("'", "'\\''")
        parts.append(f"  '{safe_url}'")

        click.echo(" \\\n".join(parts))


@cli.command(name="rebuild-index")
@click.option("--db", "db_path", help="Path to SQLite exchange DB (defaults to config)")
@click.option("--batch-size", type=int, default=5000, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
def rebuild_index(db_path: str | None, batch_size: int, as_json: bool):
    """Rebuild derived/index tables from raw exchanges."""
    import json as _json

    with _open_db(db_path) as db:
        stats = db.rebuild_derived(batch_size=batch_size)
        if as_json:
            click.echo(_json.dumps(stats, ensure_ascii=False))
        else:
            click.echo(f"rebuilt: {stats}")


@cli.command()
@click.option("--db", "db_path", help="Path to SQLite exchange DB (defaults to config)")
@click.option("--service", "service_key", help="Filter by service key")
@click.option("--host", help="Filter by host")
@click.option("--method", help="Filter by HTTP method")
@click.option("--status", type=int, help="Filter by status code")
@click.option("--path-contains", help="Filter by substring match on path")
@click.option("--limit", type=int, default=100, show_default=True)
@click.option("--offset", type=int, default=0, show_default=True, help="Skip first N results")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
def search(
    db_path: str | None,
    service_key: str | None,
    host: str | None,
    method: str | None,
    status: int | None,
    path_contains: str | None,
    limit: int,
    offset: int,
    as_json: bool,
):
    """Search captured exchanges in the SQLite DB."""
    import json as _json

    with _open_db(db_path) as db:
        resolved_key = None
        if service_key:
            resolved_key = db.resolve_service_key(service_key) or service_key
        results = db.search_exchanges(
            service_key=resolved_key,
            host=host,
            method=method.upper() if method else None,
            status=status,
            path_contains=path_contains,
            limit=limit,
            offset=offset,
        )
        if as_json:
            click.echo(_json.dumps(results, ensure_ascii=False))
        else:
            for r in results:
                # Format size
                resp_len = r.get("resp_body_len")
                if resp_len is not None and isinstance(resp_len, (int, float)):
                    resp_len = int(resp_len)
                    if resp_len >= 1_000_000:
                        size_str = f"{resp_len / 1_000_000:.1f}M"
                    elif resp_len >= 1_000:
                        size_str = f"{resp_len / 1_000:.1f}K"
                    else:
                        size_str = f"{resp_len}B"
                else:
                    size_str = ""
                # GraphQL operation
                gql_op = r.get("graphql_operation")
                gql_str = f" ({gql_op})" if gql_op else ""
                click.echo(
                    f"{r['id']:>6} {r['status']!s:>3} {r['method']:<6} "
                    f"{r.get('host', '')}{r.get('path', '')}{gql_str}"
                    f" {size_str}"
                )


@cli.command()
@click.option("--db", "db_path", help="Path to SQLite exchange DB (defaults to config)")
@click.option("--service", help="Filter by service key (substring match)")
@click.option("--method", help="Filter by HTTP method")
@click.option("--path-contains", help="Filter by substring match on path template")
@click.option("--limit", type=int, default=200, show_default=True)
@click.option("--offset", type=int, default=0, show_default=True, help="Skip first N results")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
def endpoints(
    db_path: str | None,
    service: str | None,
    method: str | None,
    path_contains: str | None,
    limit: int,
    offset: int,
    as_json: bool,
):
    """List discovered endpoint templates (DB-backed)."""
    import json as _json

    with _open_db(db_path) as db:
        results = db.endpoint_summaries(
            service=service,
            method=method.upper() if method else None,
            path_contains=path_contains,
            limit=limit,
            offset=offset,
        )
        if as_json:
            click.echo(_json.dumps(results, ensure_ascii=False))
        else:
            for r in results:
                count_obj = r.get("count")
                count = int(count_obj) if isinstance(count_obj, (int, float, str)) else 0
                sr_obj = r.get("success_rate")
                success_rate = float(sr_obj) if isinstance(sr_obj, (int, float, str)) else 0.0
                sr = success_rate * 100.0
                click.echo(
                    f"{r['service_key']:<30} {r['method']:<6} {r['path_template']:<60} "
                    f"n={count:>5} ok={sr:>5.1f}% "
                    f"ex={r.get('example_exchange_id')}"
                )


@cli.command(name="js-endpoints")
@click.option("--db", "db_path", help="Path to SQLite exchange DB (defaults to config)")
@click.option("--host", help="Restrict JS scan to a host")
@click.option("--service", help="Restrict JS scan to a service key (substring match)")
@click.option("--limit-assets", type=int, default=200, show_default=True, help="JS assets to scan")
@click.option("--limit", type=int, default=200, show_default=True, help="Max endpoints returned")
@click.option("--include", help="Only include matches containing this substring (case-insensitive)")
@click.option("--exclude", help="Exclude matches containing this substring (case-insensitive)")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
def js_endpoints(
    db_path: str | None,
    host: str | None,
    service: str | None,
    limit_assets: int,
    limit: int,
    include: str | None,
    exclude: str | None,
    as_json: bool,
):
    """Extract endpoint/URL hints from captured JS bundles (LinkFinder-style)."""
    import json as _json

    with _open_db(db_path) as db:
        rows = db.discover_js_endpoints(
            host=host,
            service=service,
            limit_assets=limit_assets,
            limit=limit,
            include=include,
            exclude=exclude,
        )
        if as_json:
            click.echo(_json.dumps(rows, ensure_ascii=False))
        else:
            for r in rows:
                c = r.get("count")
                count = int(c) if isinstance(c, int) else 0
                click.echo(f"{count:>5}  {r['endpoint']}")


@cli.command()
@click.option("--db", "db_path", help="Path to SQLite exchange DB (defaults to config)")
@click.option("--host", help="Restrict JS scan to a host")
@click.option("--service", help="Restrict JS scan to a service key (substring match)")
@click.option("--limit-assets", type=int, default=200, show_default=True, help="JS assets to scan")
@click.option("--limit", type=int, default=500, show_default=True, help="Max links returned")
@click.option("--regex", "filter_regex", help="Filter extracted links (same idea as LinkFinder -r)")
@click.option(
    "--beautify", is_flag=True, help="Beautify JS before scanning (requires motim[linkfinder])"
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
def linkfinder(
    db_path: str | None,
    host: str | None,
    service: str | None,
    limit_assets: int,
    limit: int,
    filter_regex: str | None,
    beautify: bool,
    as_json: bool,
):
    """Extract endpoints/URLs from captured JS bundles (LinkFinder-style)."""
    import json as _json

    with _open_db(db_path) as db:
        rows = db.linkfinder_js(
            host=host,
            service=service,
            limit_assets=limit_assets,
            limit=limit,
            filter_regex=filter_regex,
            beautify=beautify,
        )
        if as_json:
            click.echo(_json.dumps(rows, ensure_ascii=False))
        else:
            for r in rows:
                c = r.get("count")
                count = int(c) if isinstance(c, int) else 0
                click.echo(f"{count:>5}  {r['link']}")


@cli.command()
@click.argument("exchange_id", type=int)
@click.option("--db", "db_path", help="Path to SQLite exchange DB (defaults to config)")
@click.option("--tag", help="Tag this replay run (stored in DB)")
@click.option(
    "--transport",
    type=click.Choice(["httpx", "curl"], case_sensitive=False),
    default="httpx",
    show_default=True,
    help="HTTP transport for replay (curl uses curl_cffi)",
)
@click.option("--impersonate", help="curl transport only: browser TLS fingerprint (e.g. chrome)")
@click.option("--origin", help="Override origin, e.g. https://example.com")
@click.option("--set-header", "set_headers", multiple=True, help="Header override NAME=VALUE")
@click.option("--drop-header", "drop_headers", multiple=True, help="Drop header by name")
@click.option(
    "--body-file",
    type=click.Path(exists=True, dir_okay=False),
    help="Replace body from file",
)
@click.option(
    "--patch-json",
    "json_patches",
    multiple=True,
    help="JSON merge patch to apply to body",
)
@click.option(
    "--patch-file",
    type=click.Path(exists=True, dir_okay=False),
    help="Read JSON merge patch from file",
)
@click.option("--patch-stdin", is_flag=True, help="Read JSON merge patch from stdin")
@click.option(
    "--keep-hop-headers",
    is_flag=True,
    help="Keep hop-by-hop headers like Host/Content-Length",
)
@click.option("--timeout", type=float, default=30.0, show_default=True)
@click.option("--http2/--no-http2", default=True, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
def replay(
    exchange_id: int,
    db_path: str | None,
    tag: str | None,
    transport: str,
    impersonate: str | None,
    origin: str | None,
    set_headers: tuple[str, ...],
    drop_headers: tuple[str, ...],
    body_file: str | None,
    json_patches: tuple[str, ...],
    patch_file: str | None,
    patch_stdin: bool,
    keep_hop_headers: bool,
    timeout: float,
    http2: bool,
    as_json: bool,
):
    """Replay a captured exchange and store the result back into the DB."""
    import json as _json
    import sys

    with _open_db(db_path) as db:
        body = None
        if body_file:
            body = Path(body_file).read_bytes()
        patches = []
        if json_patches:
            for p in json_patches:
                patches.append(_json.loads(p))
        if patch_file:
            patches.append(_json.loads(Path(patch_file).read_text()))
        if patch_stdin:
            patches.append(_json.loads(sys.stdin.read()))
        result = replay_exchange(
            db,
            exchange_id,
            tag=tag,
            transport=transport,
            impersonate=impersonate,
            origin=origin,
            set_headers=set_headers,
            drop_headers=drop_headers,
            body=body,
            json_patches=patches,
            keep_hop_by_hop=keep_hop_headers,
            timeout=timeout,
            http2=http2,
        )
        payload = {
            "original_id": result.original_id,
            "replay_id": result.replay_id,
            "replay_record_id": result.replay_record_id,
            "status": result.status,
            "url": result.url,
            "notes": result.notes,
        }
        if as_json:
            click.echo(_json.dumps(payload, ensure_ascii=False))
        else:
            click.echo(f"replayed {result.original_id} -> {result.replay_id} ({result.status})")


@cli.command()
@click.argument("a_id", type=int)
@click.argument("b_id", type=int)
@click.option("--db", "db_path", help="Path to SQLite exchange DB (defaults to config)")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
def diff(a_id: int, b_id: int, db_path: str | None, as_json: bool):
    """Diff two exchanges from the SQLite DB."""
    import json as _json

    with _open_db(db_path) as db:
        a = db.get_exchange(a_id)
        b = db.get_exchange(b_id)
        d = diff_exchanges(a, b)
        if as_json:
            click.echo(_json.dumps(d, ensure_ascii=False))
        else:
            click.echo(f"{a_id} -> {b_id}: {a.get('status')} -> {b.get('status')}")


@cli.command()
@click.argument("exchange_id", type=int)
@click.option("--db", "db_path", help="Path to SQLite exchange DB (defaults to config)")
@click.option("--tag", help="Tag applied to all probe runs")
@click.option(
    "--transport",
    type=click.Choice(["httpx", "curl"], case_sensitive=False),
    default="httpx",
    show_default=True,
    help="HTTP transport for replay (curl uses curl_cffi)",
)
@click.option("--impersonate", help="curl transport only: browser TLS fingerprint (e.g. chrome)")
@click.option(
    "--patch-json",
    "json_patches",
    multiple=True,
    help="JSON merge patch (each one is a run)",
)
@click.option(
    "--patch-file",
    type=click.Path(exists=True, dir_okay=False),
    help="Read JSON merge patch from file (added as a run)",
)
@click.option(
    "--patch-stdin", is_flag=True, help="Read JSON merge patch from stdin (added as a run)"
)
@click.option(
    "--drop-header",
    "drop_headers",
    multiple=True,
    help="Drop header by name (each one is a run)",
)
@click.option("--origin", help="Override origin, e.g. https://example.com")
@click.option("--timeout", type=float, default=30.0, show_default=True)
@click.option("--http2/--no-http2", default=True, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
def probe(
    exchange_id: int,
    db_path: str | None,
    tag: str | None,
    transport: str,
    impersonate: str | None,
    json_patches: tuple[str, ...],
    patch_file: str | None,
    patch_stdin: bool,
    drop_headers: tuple[str, ...],
    origin: str | None,
    timeout: float,
    http2: bool,
    as_json: bool,
):
    """Run multiple replay mutations against a baseline and summarize diffs.

    Automatically replays a baseline first, then runs each mutation and diffs
    against the baseline. Each --patch-json value is a separate run, and each
    --drop-header value is a separate run.
    """
    import json as _json
    import sys

    with _open_db(db_path) as db:
        # Replay a baseline first
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

        runs: list[dict[str, object]] = []

        all_patches = list(json_patches or ())
        if patch_file:
            all_patches.append(Path(patch_file).read_text())
        if patch_stdin:
            all_patches.append(sys.stdin.read())

        for p in all_patches:
            patch_obj = _json.loads(p)
            r = replay_exchange(
                db,
                exchange_id,
                tag=tag,
                transport=transport,
                impersonate=impersonate,
                origin=origin,
                json_patches=[patch_obj],
                timeout=timeout,
                http2=http2,
            )
            run_ex = db.get_exchange(r.replay_id)
            d = diff_exchanges(baseline_ex, run_ex)
            runs.append(
                {
                    "kind": "patch_json",
                    "patch": patch_obj,
                    "replay_id": r.replay_id,
                    "status": r.status,
                    "diff": d,
                }
            )

        for h in drop_headers or ():
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
            d = diff_exchanges(baseline_ex, run_ex)
            runs.append(
                {
                    "kind": "drop_header",
                    "header": h,
                    "replay_id": r.replay_id,
                    "status": r.status,
                    "diff": d,
                }
            )

        payload = {
            "original_id": exchange_id,
            "baseline_id": baseline.replay_id,
            "baseline_status": baseline.status,
            "runs": runs,
        }
        if as_json:
            click.echo(_json.dumps(payload, ensure_ascii=False))
        else:
            click.echo(
                f"probe base={exchange_id} baseline={baseline.replay_id}"
                f" ({baseline.status}) runs={len(runs)}"
            )
            for run in runs:
                status_changed = run["status"] != baseline.status
                marker = " *" if status_changed else ""
                click.echo(f"  {run['kind']}: {run['status']} -> {run['replay_id']}{marker}")


@cli.command()
@click.argument("exchange_id", type=int)
@click.option("--db", "db_path", help="Path to SQLite exchange DB (defaults to config)")
@click.option(
    "--window",
    type=float,
    default=10.0,
    show_default=True,
    help="Seconds before/after seed",
)
@click.option("--service", "service_key", help="Force service key (defaults to seed's service)")
@click.option("--limit", type=int, default=200, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
def around(
    exchange_id: int,
    db_path: str | None,
    window: float,
    service_key: str | None,
    limit: int,
    as_json: bool,
):
    """Show a time-window slice of exchanges around a seed exchange."""
    import json as _json

    with _open_db(db_path) as db:
        rows = db.exchanges_around(
            exchange_id,
            window_seconds=window,
            service_key=service_key,
            limit=limit,
        )
        if as_json:
            click.echo(_json.dumps(rows, ensure_ascii=False))
        else:
            click.echo(f"around {exchange_id} window={window}s n={len(rows)}")
            for r in rows:
                click.echo(
                    f"{r['id']:>6} {r.get('status', '')!s:>3} {r.get('method', ''):<6} "
                    f"{r.get('host', '')}{r.get('path', '')}"
                )


@cli.command()
@click.argument("exchange_id", type=int)
@click.option("--db", "db_path", help="Path to SQLite exchange DB (defaults to config)")
@click.option(
    "--gap",
    type=float,
    default=120.0,
    show_default=True,
    help="Gap seconds to split sessions",
)
@click.option(
    "--id-window",
    type=int,
    default=5000,
    show_default=True,
    help="ID window around seed",
)
@click.option("--limit", type=int, default=500, show_default=True, help="Max items returned")
@click.option("--no-filter-noise", is_flag=True, help="Disable noise filtering")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
def session(
    exchange_id: int,
    db_path: str | None,
    gap: float,
    id_window: int,
    limit: int,
    no_filter_noise: bool,
    as_json: bool,
):
    """Return a best-effort session slice around an exchange."""
    import json as _json

    with _open_db(db_path) as db:
        payload = db.session_slice(
            exchange_id,
            gap_seconds=gap,
            id_window=id_window,
            limit=limit,
            filter_noise=not no_filter_noise,
        )
        if as_json:
            click.echo(_json.dumps(payload, ensure_ascii=False))
        else:
            items_obj = payload.get("items")
            items = items_obj if isinstance(items_obj, list) else []
            click.echo(
                f"session {exchange_id} service={payload.get('service_key')} "
                f"n={len(items)} gap={gap}s"
            )
            for item in items[: min(25, len(items))]:
                click.echo(
                    f"{item['id']:>6} {item.get('status', '')!s:>3} {item.get('method', ''):<6} "
                    f"{item.get('host', '')}{item.get('path', '')}"
                )
            if len(items) > 25:
                click.echo(f"... {len(items) - 25} more")


@cli.command(name="replay-seq")
@click.argument("exchange_ids", type=int, nargs=-1)
@click.option("--db", "db_path", help="Path to SQLite exchange DB (defaults to config)")
@click.option("--tag", help="Tag applied to all replay runs")
@click.option(
    "--transport",
    type=click.Choice(["httpx", "curl"], case_sensitive=False),
    default="httpx",
    show_default=True,
    help="HTTP transport for replay (curl uses curl_cffi)",
)
@click.option("--impersonate", help="curl transport only: browser TLS fingerprint (e.g. chrome)")
@click.option("--origin", help="Override origin, e.g. https://example.com")
@click.option("--timeout", type=float, default=30.0, show_default=True)
@click.option("--http2/--no-http2", default=True, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
def replay_seq(
    exchange_ids: tuple[int, ...],
    db_path: str | None,
    tag: str | None,
    transport: str,
    impersonate: str | None,
    origin: str | None,
    timeout: float,
    http2: bool,
    as_json: bool,
):
    """Replay a sequence of exchanges in order (stores all results back into DB)."""
    import json as _json

    if not exchange_ids:
        raise click.UsageError("Provide at least one exchange id")

    with _open_db(db_path) as db:
        out: list[dict[str, object]] = []
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
            out.append({"original_id": int(eid), "replay_id": r.replay_id, "status": r.status})
        if as_json:
            click.echo(_json.dumps(out, ensure_ascii=False))
        else:
            for item in out:
                click.echo(f"{item['original_id']} -> {item['replay_id']} ({item['status']})")


@cli.command(name="export-yaml")
@click.argument("service")
@click.option("--db", "db_path", help="Path to SQLite exchange DB (defaults to config)")
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False),
    help="Output YAML path (default: ~/.motim/exports/<service>.yaml)",
)
def export_yaml(service: str, db_path: str | None, out_path: str | None):
    """Export a lightweight YAML summary from the SQLite DB (optional artifact)."""
    from urllib.parse import urlparse

    import yaml

    with _open_db(db_path) as db:
        skey = db.resolve_service_key(service) or service
        origin = db.latest_origin(skey) or ""
        host = urlparse(origin).hostname if origin else None

        endpoints = db.endpoint_summaries(service=skey, limit=10_000)
        endpoint_lines = [f"{e['method']} {e['path_template']}" for e in endpoints]

        snap = db.latest_auth_snapshot(skey)
        auth_headers = (snap or {}).get("headers") if snap else None

        doc = {
            "service": host or skey.replace("_", "."),
            "base_url": origin or (f"https://{host}" if host else ""),
            "auth": {
                "headers": auth_headers or {},
                "last_seen": (snap or {}).get("ts") if snap else None,
            },
            "observed_endpoints": endpoint_lines,
            "meta": {
                "generated_from": "exchange_db",
                "service_key": skey,
            },
        }

        dest = (
            Path(out_path).expanduser()
            if out_path
            else (Path.home() / ".motim" / "exports" / f"{skey}.yaml")
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
        click.echo(str(dest))


@cli.command()
@click.option(
    "--port",
    "-p",
    default=8080,
    type=click.IntRange(1, 65535),
    help="Proxy port (default: 8080)",
)
@click.option("--verbose", "-v", is_flag=True, help="Show all requests (including skipped)")
@click.pass_context
def start(ctx: click.Context, port: int, verbose: bool):
    """Alias for `motim proxy start`."""
    ctx.invoke(proxy_start, port=port, verbose=verbose)


@cli.command()
@click.pass_context
def stop(ctx: click.Context):
    """Alias for `motim proxy stop`."""
    ctx.invoke(proxy_stop)


@cli.command()
@click.pass_context
def status(ctx: click.Context):
    """Alias for `motim proxy status`."""
    ctx.invoke(proxy_status)


@cli.command(name="trust-cert")
@click.pass_context
def trust_cert(ctx: click.Context):
    """Alias for `motim proxy trust-cert`."""
    ctx.invoke(proxy_trust_cert)


@cli.command()
@click.option("--skip-cert", is_flag=True, help="Skip certificate setup")
def init(skip_cert: bool):
    """Initialize MOTIM (create dirs, install CA cert, install skill).

    This command:
    1. Creates ~/.motim/specs/ directory
    2. Creates default config if not exists
    3. Generates and trusts the mitmproxy CA certificate
    4. Installs the agent skill file
    """
    import platform
    import subprocess
    import time
    from pathlib import Path

    MOTIM_DIR = Path.home() / ".motim"
    SPECS_DIR = MOTIM_DIR / "specs"
    CONFIG_FILE = MOTIM_DIR / "config.yaml"
    MITMPROXY_DIR = Path.home() / ".mitmproxy"
    CA_CERT = MITMPROXY_DIR / "mitmproxy-ca-cert.pem"
    SKILL_SOURCE = Path(__file__).parent.parent / "skill.md"
    SKILL_DIR = Path.home() / ".claude" / "skills" / "motim"
    SKILL_DEST = SKILL_DIR / "SKILL.md"
    DEFAULT_CONFIG = Path(__file__).parent.parent / "default_config.yaml"

    click.echo("=" * 60)
    click.echo("MOTIM Initialization")
    click.echo("=" * 60)

    # Step 1: Create directories
    click.echo("\n[1/4] Creating directories...")
    MOTIM_DIR.mkdir(parents=True, exist_ok=True)
    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    click.echo(f"       {MOTIM_DIR} ✓")

    # Step 2: Create default config if not exists
    click.echo("\n[2/4] Setting up configuration...")
    if not CONFIG_FILE.exists() and DEFAULT_CONFIG.exists():
        CONFIG_FILE.write_text(DEFAULT_CONFIG.read_text())
        click.echo(f"       {CONFIG_FILE} ✓ (created)")
    else:
        click.echo(f"       {CONFIG_FILE} ✓ (exists)")

    # Step 3: Certificate setup
    click.echo("\n[3/4] Certificate setup...")
    if skip_cert:
        click.echo("       Skipped (use 'motim proxy trust-cert' later)")
    else:
        # Generate cert if needed
        if not CA_CERT.exists():
            click.echo("       Generating CA certificate...")
            try:
                process = subprocess.Popen(
                    ["mitmdump", "--quiet"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                time.sleep(2)
                process.terminate()
                process.wait(timeout=5)
            except Exception as e:
                click.echo(f"       ✗ Failed to generate certificate: {e}", err=True)

        if CA_CERT.exists():
            click.echo(f"       {CA_CERT} ✓")

            # Trust certificate based on platform
            system = platform.system()
            if system == "Darwin":
                click.echo("       Installing to System keychain (requires sudo)...")
                try:
                    subprocess.run(
                        [
                            "sudo",
                            "security",
                            "add-trusted-cert",
                            "-d",
                            "-r",
                            "trustRoot",
                            "-k",
                            "/Library/Keychains/System.keychain",
                            str(CA_CERT),
                        ],
                        check=True,
                    )
                    click.echo("       Certificate trusted ✓")
                except subprocess.CalledProcessError:
                    click.echo("       ✗ Failed to trust certificate", err=True)

            elif system == "Linux":
                ca_dest = Path("/usr/local/share/ca-certificates/mitmproxy-ca-cert.crt")
                click.echo("       Installing certificate (requires sudo)...")
                try:
                    subprocess.run(["sudo", "cp", str(CA_CERT), str(ca_dest)], check=True)
                    subprocess.run(["sudo", "update-ca-certificates"], check=True)
                    click.echo("       Certificate trusted ✓")
                except subprocess.CalledProcessError:
                    click.echo("       ✗ Failed to trust certificate", err=True)

            elif system == "Windows":
                click.echo(f"       Please manually trust: {CA_CERT}")

    # Step 4: Install skill
    click.echo("\n[4/4] Installing agent skill...")
    SKILL_DIR.mkdir(parents=True, exist_ok=True)
    if SKILL_SOURCE.exists():
        SKILL_DEST.write_text(SKILL_SOURCE.read_text())
        click.echo(f"       {SKILL_DIR}/ ✓")
    else:
        click.echo(f"       ✗ Skill source not found: {SKILL_SOURCE}", err=True)

    # Optional: print MCP config snippet if mcp extra is installed
    try:
        import importlib.util

        if importlib.util.find_spec("mcp") is not None:
            click.echo("\n[bonus] Native MCP server available.")
            click.echo("        Add this to your MCP client config (e.g. Claude Desktop's")
            click.echo("        claude_desktop_config.json) to use motim from any MCP agent:")
            click.echo("")
            click.echo("        {")
            click.echo('          "mcpServers": {')
            click.echo('            "motim": {')
            click.echo('              "command": "motim",')
            click.echo('              "args": ["mcp"]')
            click.echo("            }")
            click.echo("          }")
            click.echo("        }")
    except Exception:  # pragma: no cover - best-effort hint
        pass

    # Done
    click.echo("\n" + "=" * 60)
    click.echo("SETUP COMPLETE")
    click.echo("=" * 60)
    click.echo("""
Next steps:

  1. Start the proxy:
     motim proxy start

  2. Configure your browser/system to use localhost:8080 as proxy
     - macOS: System Preferences → Network → Advanced → Proxies
     - Or use a browser extension like FoxyProxy

  3. Browse normally - specs will appear in ~/.motim/specs/

  4. Check captured services:
     motim services

  5. For Codex / opencode / other agents, run in your project:
     motim agents-md

  6. For MCP-compatible agents (Claude Desktop, Cursor, Cline, etc.),
     install the MCP extra and use the config snippet above:
     pip install 'motim[mcp]'
""")


@cli.command()
@click.option(
    "--db",
    "db_path",
    help="Path to SQLite exchange DB (defaults to config)",
)
def mcp(db_path: str | None):
    """Run the MOTIM MCP server (stdio).

    Exposes motim's search, inspect, and replay primitives as Model
    Context Protocol tools so any MCP-compatible agent (Claude Desktop,
    Cursor, Cline, Continue, Zed, etc.) can drive motim without shelling
    out to the CLI.

    Requires: pip install 'motim[mcp]'

    \b
    Example Claude Desktop config (~/Library/Application Support/Claude/claude_desktop_config.json):

        {
          "mcpServers": {
            "motim": {
              "command": "motim",
              "args": ["mcp"]
            }
          }
        }
    """
    try:
        from motim.mcp.server import build_server
    except ImportError as e:
        raise click.ClickException(
            "MCP support is not installed. Install with: pip install 'motim[mcp]'"
        ) from e

    server = build_server(db_path=db_path)
    server.run("stdio")


@cli.command(name="agents-md")
@click.argument("directory", default=".", type=click.Path(exists=True, file_okay=False))
def agents_md(directory: str):
    """Write AGENTS.md to a project directory for Codex / opencode / other agents.

    This copies the MOTIM skill file (stripped of Claude-specific frontmatter)
    into AGENTS.md so that non-Claude agents can discover it.

    \b
    Examples:
        motim agents-md          # Write to current directory
        motim agents-md ./myapp  # Write to a specific project
    """
    from pathlib import Path

    skill_source = Path(__file__).parent.parent / "skill.md"
    dest = Path(directory).resolve() / "AGENTS.md"

    if not skill_source.exists():
        click.echo(f"Error: skill source not found: {skill_source}", err=True)
        raise SystemExit(1)

    content = skill_source.read_text()

    # Strip YAML frontmatter (Claude-specific metadata)
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3 :].lstrip("\n")

    dest.write_text(content)
    click.echo(f"Wrote {dest}")
    click.echo("Codex, opencode, and other AGENTS.md-compatible tools will now discover MOTIM.")


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
def doctor(as_json: bool):
    """Check system health and configuration."""
    import json as _json
    from importlib.util import find_spec
    from pathlib import Path

    checks = []

    # Check directories
    MOTIM_DIR = Path.home() / ".motim"
    SPECS_DIR = MOTIM_DIR / "specs"

    checks.append(("~/.motim directory", MOTIM_DIR.exists()))
    checks.append(("~/.motim/specs directory", SPECS_DIR.exists()))

    # Check config
    CONFIG_FILE = MOTIM_DIR / "config.yaml"
    checks.append(("Config file", CONFIG_FILE.exists()))

    # Check certificate
    CA_CERT = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
    checks.append(("CA certificate", CA_CERT.exists()))

    # Check skill (Claude Code)
    SKILL_DEST = Path.home() / ".claude" / "skills" / "motim" / "SKILL.md"
    checks.append(("Claude Code skill", SKILL_DEST.exists()))

    # Check AGENTS.md (Codex / opencode)
    AGENTS_MD = Path.cwd() / "AGENTS.md"
    checks.append(("AGENTS.md (cwd)", AGENTS_MD.exists()))

    # Check mitmproxy
    import shutil

    checks.append(("mitmproxy installed", shutil.which("mitmdump") is not None))

    # Check httpx
    checks.append(("httpx installed", find_spec("httpx") is not None))

    # Check MCP (optional)
    checks.append(("mcp installed (optional)", find_spec("mcp") is not None))

    spec_count = len(list(SPECS_DIR.glob("*.yaml"))) if SPECS_DIR.exists() else 0
    # Optional checks (presence is informational, not required for all_ok)
    OPTIONAL = {"mcp installed (optional)"}
    all_ok = all(ok for name, ok in checks if name not in OPTIONAL)

    if as_json:
        payload = {
            "checks": {name: ok for name, ok in checks},
            "spec_count": spec_count,
            "all_ok": all_ok,
        }
        click.echo(_json.dumps(payload, ensure_ascii=False))
        return

    click.echo("MOTIM Health Check")
    click.echo("=" * 40)

    # Print results
    for name, ok in checks:
        status_icon = "✓" if ok else "✗"
        click.echo(f"  {status_icon} {name}")

    if SPECS_DIR.exists():
        click.echo(f"\n  {spec_count} service(s) captured")

    click.echo("\n" + ("All checks passed!" if all_ok else "Some checks failed."))

    if not all_ok:
        click.echo("Run 'motim init' to fix missing components.")


if __name__ == "__main__":
    cli()
