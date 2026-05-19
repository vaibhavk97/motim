"""SQLite-backed exchange database for agent workflows.

This is the "Burp-like" substrate for agents:
- store full request/response exchanges (raw bytes + headers + metadata)
- support fast search and retrieval for replay/diffing

It intentionally avoids heavy dependencies and uses SQLite (single-file).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

from .normalize import format_cookie_header, parse_cookie_header, templatize_path


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(data: bytes | None) -> str | None:
    if data is None:
        return None
    return hashlib.sha256(data).hexdigest()


_JS_URL_RE = re.compile(r"https?://[^\s\"'`<>]{6,300}")
_JS_WS_URL_RE = re.compile(r"wss?://[^\s\"'`<>]{6,300}")
# LinkFinder-ish: common API-ish absolute paths inside JS bundles.
_JS_API_PATH_RE = re.compile(r"/(?:api|graphql|gql)/[A-Za-z0-9/_\-\.]{2,300}")
_JS_PATH_RE = re.compile(r"/[A-Za-z0-9/_\-\.]{3,300}")
_JS_BAD_EXTENSIONS = (
    ".js",
    ".mjs",
    ".css",
    ".map",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp4",
    ".webm",
    ".mp3",
)


def _clean_js_extracted(s: str) -> str | None:
    s = s.strip().strip(",;)")
    if not s:
        return None
    if len(s) < 5:
        return None
    # Trim trailing quotes if any slipped through.
    s = s.strip("\"'`")
    # Avoid obvious non-endpoints.
    low = s.lower()
    if low.startswith("data:"):
        return None
    if any(low.endswith(ext) for ext in _JS_BAD_EXTENSIONS):
        return None
    # Too generic.
    if s in {"/", "/api", "/api/"}:
        return None
    return s


def _extract_from_js_text(text: str) -> Iterable[str]:
    # Prefer API-looking paths; then URLs; then generic paths.
    for m in _JS_API_PATH_RE.finditer(text):
        out = _clean_js_extracted(m.group(0))
        if out:
            yield out
    for m in _JS_URL_RE.finditer(text):
        out = _clean_js_extracted(m.group(0))
        if out:
            yield out
    for m in _JS_WS_URL_RE.finditer(text):
        out = _clean_js_extracted(m.group(0))
        if out:
            yield out
    # Generic paths are noisy; only keep if they look API-ish.
    for m in _JS_PATH_RE.finditer(text):
        s = m.group(0)
        if "/api/" not in s and "/graphql" not in s and "/gql" not in s:
            continue
        out = _clean_js_extracted(s)
        if out:
            yield out


@dataclass(frozen=True)
class HeaderField:
    name: str
    value: str


class ExchangeDB:
    """SQLite exchange database."""

    def __init__(self, path: Path, *, max_body_bytes: int = 1_000_000):
        self.path = Path(path).expanduser()
        self.max_body_bytes = max_body_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ExchangeDB":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def service_summaries(self, *, limit: int | None = None) -> list[dict[str, object]]:
        """Return per-host service summaries for fast discovery.

        This is intended to power `motim services` without parsing YAML specs.

        Returns:
            List of dicts with keys:
            - service_key: str
            - host: str | None
            - exchanges: int
            - endpoints: int
            - last_ts: str
            - has_auth: bool
        """
        cur = self._conn.cursor()
        try:
            sql = """
            SELECT service_key, host, exchange_count AS exchanges, endpoint_count AS endpoints,
                   last_ts, has_auth
              FROM services_index
             WHERE last_ts IS NOT NULL
             ORDER BY last_ts DESC
            """
            params: tuple[object, ...] = ()
            if limit is not None:
                sql += " LIMIT ?"
                params = (int(limit),)
            rows = cur.execute(sql, params).fetchall()
            return [
                {
                    "service_key": str(r["service_key"]),
                    "host": r["host"],
                    "exchanges": int(r["exchanges"] or 0),
                    "endpoints": int(r["endpoints"] or 0),
                    "last_ts": str(r["last_ts"]),
                    "has_auth": bool(int(r["has_auth"] or 0)),
                }
                for r in rows
            ]
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def endpoint_summaries(
        self,
        *,
        service: str | None = None,
        method: str | None = None,
        path_contains: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, object]]:
        """List endpoint templates with counts/success for fast discovery."""
        cur = self._conn.cursor()
        try:
            clauses: list[str] = []
            params: list[object] = []

            if service:
                clauses.append("service_key LIKE ?")
                params.append(f"%{service}%")
            if method:
                clauses.append("method = ?")
                params.append(method)
            if path_contains:
                clauses.append("path_template LIKE ?")
                params.append(f"%{path_contains}%")

            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

            q = (
                "SELECT service_key, method, path_template, count, success_count, "
                "last_ts, last_status, example_exchange_id "
                "FROM endpoints_index"
                f"{where} ORDER BY last_ts DESC, count DESC LIMIT ? OFFSET ?"
            )
            params.append(int(limit))
            params.append(int(offset))

            rows = cur.execute(q, params).fetchall()
            out: list[dict[str, object]] = []
            for r in rows:
                count = int(r["count"] or 0)
                success = int(r["success_count"] or 0)
                out.append(
                    {
                        "service_key": str(r["service_key"]),
                        "method": str(r["method"]),
                        "path_template": str(r["path_template"]),
                        "count": count,
                        "success_count": success,
                        "success_rate": (success / count) if count else 0.0,
                        "last_ts": r["last_ts"],
                        "last_status": r["last_status"],
                        "example_exchange_id": r["example_exchange_id"],
                    }
                )
            return out
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def resolve_service_key(self, query: str) -> str | None:
        """Resolve a user query to a concrete service_key.

        Prefers `services_index`.  Normalizes dots/colons to underscores so
        that ``api.example.com`` matches ``api_example_com``.
        """
        q = query.strip()
        if not q:
            return None
        normalized = q.replace(".", "_").replace(":", "_")
        cur = self._conn.cursor()
        try:
            # 1. Exact match with original query
            row = cur.execute(
                "SELECT service_key FROM services_index WHERE service_key = ?",
                (q,),
            ).fetchone()
            if row is not None:
                return str(row["service_key"])

            # 2. Exact match with normalized query
            if normalized != q:
                row = cur.execute(
                    "SELECT service_key FROM services_index WHERE service_key = ?",
                    (normalized,),
                ).fetchone()
                if row is not None:
                    return str(row["service_key"])

            # 3. LIKE match with original query
            row = cur.execute(
                "SELECT service_key FROM services_index "
                "WHERE service_key LIKE ? "
                "ORDER BY last_ts DESC "
                "LIMIT 1",
                (f"%{q}%",),
            ).fetchone()
            if row is not None:
                return str(row["service_key"])

            # 4. LIKE match with normalized query
            if normalized != q:
                row = cur.execute(
                    "SELECT service_key FROM services_index "
                    "WHERE service_key LIKE ? "
                    "ORDER BY last_ts DESC "
                    "LIMIT 1",
                    (f"%{normalized}%",),
                ).fetchone()
                if row is not None:
                    return str(row["service_key"])

            return None
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def latest_auth_snapshot(self, service_key: str) -> dict[str, object] | None:
        """Return the most recent auth snapshot for a service_key."""
        skey = self.resolve_service_key(service_key) or service_key
        cur = self._conn.cursor()
        try:
            row = cur.execute(
                """
                SELECT id, service_key, ts, cookie_header, cookie_hash,
                       headers_json, source_exchange_id
                  FROM auth_snapshots
                 WHERE service_key = ?
                 ORDER BY ts DESC, id DESC
                 LIMIT 1
                """,
                (skey,),
            ).fetchone()
            if row is None:
                return None
            headers_json = row["headers_json"]
            headers: dict[str, str] = {}
            if isinstance(headers_json, str) and headers_json:
                try:
                    parsed = json.loads(headers_json)
                    if isinstance(parsed, dict):
                        headers = {str(k): str(v) for k, v in parsed.items()}
                except Exception:
                    headers = {}
            return {
                "id": int(row["id"]),
                "service_key": str(row["service_key"]),
                "ts": str(row["ts"]),
                "cookie_header": row["cookie_header"],
                "cookie_hash": row["cookie_hash"],
                "headers": headers,
                "source_exchange_id": row["source_exchange_id"],
            }
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def latest_origin(self, service_key: str) -> str | None:
        """Best-effort origin (scheme://host[:port]) for a service_key."""
        skey = self.resolve_service_key(service_key) or service_key
        cur = self._conn.cursor()
        try:
            row = cur.execute(
                """
                SELECT scheme, host, port
                  FROM exchanges
                 WHERE service_key = ? OR REPLACE(COALESCE(host,''),'.','_') = ?
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (skey, skey),
            ).fetchone()
            if row is None:
                return None
            scheme = str(row["scheme"] or "https")
            host = str(row["host"] or "")
            if not host:
                return None
            port = row["port"]
            if port and int(port) not in (80, 443):
                return f"{scheme}://{host}:{int(port)}"
            return f"{scheme}://{host}"
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def record_replay(
        self,
        *,
        original_exchange_id: int,
        replay_exchange_id: int,
        ts: str | None = None,
        tag: str | None = None,
        origin: str | None = None,
        set_headers: Sequence[str] = (),
        drop_headers: Sequence[str] = (),
        json_patches: Sequence[object] = (),
        notes: Sequence[str] = (),
    ) -> int:
        """Persist metadata about a replay run."""
        ts = ts or _utcnow_iso()
        cur = self._conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO replays(
                  ts, tag, original_exchange_id, replay_exchange_id,
                  origin, set_headers_json, drop_headers_json, patch_json_json, notes_json
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    ts,
                    tag,
                    int(original_exchange_id),
                    int(replay_exchange_id),
                    origin,
                    json.dumps(list(set_headers), ensure_ascii=False, separators=(",", ":")),
                    json.dumps(list(drop_headers), ensure_ascii=False, separators=(",", ":")),
                    json.dumps(list(json_patches), ensure_ascii=False, separators=(",", ":")),
                    json.dumps(list(notes), ensure_ascii=False, separators=(",", ":")),
                ),
            )
            self._conn.commit()
            if cur.lastrowid is None:  # pragma: no cover
                raise RuntimeError("SQLite insert did not return lastrowid")
            return int(cur.lastrowid)
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def delete_service(self, service: str) -> dict[str, int]:
        """Delete all data for a service_key (and its indexes)."""
        skey = self.resolve_service_key(service) or service
        cur = self._conn.cursor()
        try:
            # Delete raw exchanges (cascades to headers/bodies, and replays FK cascades).
            cur.execute(
                "DELETE FROM exchanges WHERE service_key = ?"
                " OR REPLACE(COALESCE(host,''),'.','_') = ?",
                (skey, skey),
            )
            exchanges_deleted = int(cur.rowcount or 0)

            cur.execute("DELETE FROM services_index WHERE service_key = ?", (skey,))
            services_deleted = int(cur.rowcount or 0)

            cur.execute("DELETE FROM endpoints_index WHERE service_key = ?", (skey,))
            endpoints_deleted = int(cur.rowcount or 0)

            cur.execute("DELETE FROM auth_snapshots WHERE service_key = ?", (skey,))
            snapshots_deleted = int(cur.rowcount or 0)

            self._conn.commit()
            return {
                "exchanges_deleted": exchanges_deleted,
                "services_index_deleted": services_deleted,
                "endpoints_index_deleted": endpoints_deleted,
                "auth_snapshots_deleted": snapshots_deleted,
            }
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def clear_all(self) -> dict[str, int]:
        """Delete all captured data and derived indexes."""
        cur = self._conn.cursor()
        try:
            # Order matters with FKs.
            cur.execute("DELETE FROM replays;")
            replays_deleted = int(cur.rowcount or 0)
            cur.execute("DELETE FROM auth_snapshots;")
            snapshots_deleted = int(cur.rowcount or 0)
            cur.execute("DELETE FROM endpoints_index;")
            endpoints_deleted = int(cur.rowcount or 0)
            cur.execute("DELETE FROM services_index;")
            services_deleted = int(cur.rowcount or 0)
            cur.execute("DELETE FROM exchanges;")
            exchanges_deleted = int(cur.rowcount or 0)

            self._conn.commit()
            return {
                "replays_deleted": replays_deleted,
                "auth_snapshots_deleted": snapshots_deleted,
                "endpoints_index_deleted": endpoints_deleted,
                "services_index_deleted": services_deleted,
                "exchanges_deleted": exchanges_deleted,
            }
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def rebuild_derived(self, *, batch_size: int = 5000) -> dict[str, int]:
        """Rebuild derived/index tables from raw exchanges.

        This is required whenever the DB has exchanges but derived tables are empty/out-of-date.
        """
        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN;")
            cur.execute("DELETE FROM auth_snapshots;")
            cur.execute("DELETE FROM endpoints_index;")
            cur.execute("DELETE FROM services_index;")
            cur.execute("COMMIT;")
        except Exception:
            self._conn.rollback()
            raise
        finally:
            try:
                cur.close()
            except Exception:
                pass

        cur = self._conn.cursor()
        hdr_cur = self._conn.cursor()
        rebuilt = 0
        try:
            cur.execute("BEGIN;")
            rows = cur.execute(
                """
                SELECT id, ts, host, method, path, status, service_key
                  FROM exchanges
                 ORDER BY id ASC
                """
            ).fetchall()
            for r in rows:
                exchange_id = int(r["id"])
                # Load request headers for auth snapshot heuristics.
                hdr_rows = hdr_cur.execute(
                    """
                    SELECT name, value
                      FROM headers
                     WHERE exchange_id = ? AND side = 'request'
                     ORDER BY idx ASC
                    """,
                    (exchange_id,),
                ).fetchall()
                req_headers = [HeaderField(name=h["name"], value=h["value"]) for h in hdr_rows]

                self._update_indexes_no_commit(
                    cur,
                    exchange_id=exchange_id,
                    ts=str(r["ts"]),
                    host=r["host"],
                    method=str(r["method"]),
                    path=r["path"],
                    status=r["status"],
                    service_key=r["service_key"],
                    req_headers=req_headers,
                )
                rebuilt += 1
                if rebuilt % int(batch_size) == 0:
                    self._conn.commit()
                    cur.execute("BEGIN;")

            self._conn.commit()
            return {"exchanges_processed": rebuilt}
        except Exception:
            self._conn.rollback()
            raise
        finally:
            try:
                cur.close()
            except Exception:
                pass
            try:
                hdr_cur.close()
            except Exception:
                pass

    def exchanges_around(
        self,
        exchange_id: int,
        *,
        window_seconds: float = 10.0,
        service_key: str | None = None,
        limit: int = 500,
        prefer_success: bool = True,
    ) -> list[dict]:
        """Return exchanges around a seed exchange id by timestamp.

        This is the core DB primitive for time-based sequencing (no workflow graph).
        """
        cur = self._conn.cursor()
        try:
            seed = cur.execute(
                "SELECT ts, service_key, host FROM exchanges WHERE id = ?",
                (int(exchange_id),),
            ).fetchone()
            if seed is None:
                raise KeyError(exchange_id)
            ts = str(seed["ts"])
            # Compute bounds in Python to avoid SQLite datetime() timezone quirks.
            try:
                seed_dt = datetime.fromisoformat(ts)
                start_dt = seed_dt - timedelta(seconds=float(window_seconds))
                end_dt = seed_dt + timedelta(seconds=float(window_seconds))
                start_ts = start_dt.isoformat()
                end_ts = end_dt.isoformat()
            except Exception:
                # Fallback: use exact ts as center with a coarse id-window query later.
                start_ts = ts
                end_ts = ts

            clauses: list[str] = ["ts >= ? AND ts <= ?"]
            params: list[object] = [start_ts, end_ts]

            if service_key:
                clauses.append("service_key = ?")
                params.append(service_key)
            elif seed["service_key"]:
                clauses.append("service_key = ?")
                params.append(seed["service_key"])

            where = " AND ".join(clauses)
            order = (
                "ORDER BY (status BETWEEN 200 AND 299) DESC, ts ASC, id ASC"
                if prefer_success
                else "ORDER BY ts ASC, id ASC"
            )
            q = (
                "SELECT id, ts, scheme, host, port, method, path, query,"
                " url, status, endpoint, service_key "
                "FROM exchanges "
                f"WHERE {where} "
                f"{order} "
                "LIMIT ?"
            )
            params.append(int(limit))
            rows = cur.execute(q, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def exchanges_in_range(
        self,
        *,
        start_ts: str,
        end_ts: str,
        service_key: str | None = None,
        limit: int = 5000,
        prefer_success: bool = False,
    ) -> list[dict]:
        """Return exchanges in a timestamp range (inclusive)."""
        cur = self._conn.cursor()
        try:
            clauses: list[str] = ["ts >= ? AND ts <= ?"]
            params: list[object] = [start_ts, end_ts]
            if service_key:
                clauses.append("service_key = ?")
                params.append(service_key)
            where = " AND ".join(clauses)
            order = (
                "ORDER BY (status BETWEEN 200 AND 299) DESC, ts ASC, id ASC"
                if prefer_success
                else "ORDER BY ts ASC, id ASC"
            )
            q = (
                "SELECT id, ts, scheme, host, port, method, path, query,"
                " url, status, endpoint, service_key "
                "FROM exchanges "
                f"WHERE {where} "
                f"{order} "
                "LIMIT ?"
            )
            params.append(int(limit))
            rows = cur.execute(q, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def session_slice(
        self,
        exchange_id: int,
        *,
        gap_seconds: float = 120.0,
        id_window: int = 5000,
        limit: int = 2000,
        filter_noise: bool = True,
    ) -> dict[str, object]:
        """Best-effort session slice around an exchange.

        Sessions are defined by time gaps within an id-window for the same service_key.
        This is intentionally heuristic but dramatically reduces background/polling noise
        for LLM-driven chaining.
        """
        cur = self._conn.cursor()
        try:
            seed = cur.execute(
                "SELECT id, ts, service_key, host FROM exchanges WHERE id = ?",
                (int(exchange_id),),
            ).fetchone()
            if seed is None:
                raise KeyError(exchange_id)

            skey = seed["service_key"] or self._normalize_service_key(seed["host"], None)
            start_id = max(1, int(exchange_id) - int(id_window))
            end_id = int(exchange_id) + int(id_window)

            rows = cur.execute(
                """
                SELECT id, ts, host, method, path, url, status,
                       req_content_type, resp_content_type, endpoint, service_key
                  FROM exchanges
                 WHERE id BETWEEN ? AND ?
                   AND (service_key = ? OR REPLACE(COALESCE(host,''),'.','_') = ?)
                 ORDER BY ts ASC, id ASC
                """,
                (start_id, end_id, skey, skey),
            ).fetchall()

            items: list[dict] = [dict(r) for r in rows]
            if not items:
                return {"service_key": skey, "seed_id": exchange_id, "items": []}

            # Optional noise filtering (still DB-first; no workflow graph).
            if filter_noise:
                deny_path_substrings = [
                    "/log",
                    "/events",
                    "/metrics",
                    "/analytics",
                    "/promoted_content/",
                ]
                deny_methods = {"OPTIONS"}
                filtered: list[dict] = []
                for it in items:
                    method = str(it.get("method") or "").upper()
                    path = str(it.get("path") or "")
                    resp_ct = str(it.get("resp_content_type") or "")
                    if method in deny_methods:
                        continue
                    if any(s in path for s in deny_path_substrings):
                        continue
                    if any(resp_ct.startswith(p) for p in ("image/", "font/", "audio/", "video/")):
                        continue
                    filtered.append(it)
                items = filtered

            # Find segment boundaries by time gaps.
            gap = float(gap_seconds)
            times: list[datetime] = []
            for it in items:
                try:
                    times.append(datetime.fromisoformat(str(it["ts"])))
                except Exception:
                    times.append(datetime.min.replace(tzinfo=timezone.utc))

            # Locate the index of the seed exchange in the filtered list.
            seed_idx = None
            for i, it in enumerate(items):
                if int(it["id"]) == int(exchange_id):
                    seed_idx = i
                    break
            if seed_idx is None:
                # Seed might have been filtered; fall back to nearest by id.
                seed_idx = min(
                    range(len(items)), key=lambda i: abs(int(items[i]["id"]) - int(exchange_id))
                )

            left = seed_idx
            while left > 0:
                if (times[left] - times[left - 1]).total_seconds() > gap:
                    break
                left -= 1

            right = seed_idx
            while right + 1 < len(items):
                if (times[right + 1] - times[right]).total_seconds() > gap:
                    break
                right += 1

            slice_items = items[left : right + 1]
            if len(slice_items) > int(limit):
                slice_items = slice_items[-int(limit) :]

            return {
                "service_key": str(skey),
                "seed_id": int(exchange_id),
                "gap_seconds": float(gap_seconds),
                "id_window": int(id_window),
                "items": slice_items,
            }
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def _init_db(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.execute("PRAGMA foreign_keys=ON;")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS exchanges (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              scheme TEXT,
              host TEXT,
              port INTEGER,
              method TEXT NOT NULL,
              path TEXT,
              query TEXT,
              url TEXT,
              status INTEGER,
              graphql_operation TEXT,
              endpoint TEXT,
              service_key TEXT,
              req_content_type TEXT,
              resp_content_type TEXT,
              req_body_len INTEGER,
              resp_body_len INTEGER,
              req_body_sha256 TEXT,
              resp_body_sha256 TEXT,
              req_body_truncated INTEGER NOT NULL DEFAULT 0,
              resp_body_truncated INTEGER NOT NULL DEFAULT 0
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS headers (
              exchange_id INTEGER NOT NULL,
              side TEXT NOT NULL CHECK(side IN ('request','response')),
              idx INTEGER NOT NULL,
              name TEXT NOT NULL,
              value TEXT NOT NULL,
              FOREIGN KEY(exchange_id) REFERENCES exchanges(id) ON DELETE CASCADE
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_headers_exchange ON headers(exchange_id);")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bodies (
              exchange_id INTEGER NOT NULL,
              side TEXT NOT NULL CHECK(side IN ('request','response')),
              raw BLOB,
              FOREIGN KEY(exchange_id) REFERENCES exchanges(id) ON DELETE CASCADE,
              UNIQUE(exchange_id, side)
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_exchanges_ts ON exchanges(ts);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_exchanges_host_path ON exchanges(host, path);")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_exchanges_method_status ON exchanges(method, status);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_exchanges_service_key ON exchanges(service_key);"
        )

        # ─────────────────────────────────────────────────────────────────────
        # Derived/index tables (agent-first discovery + auth snapshots)
        # ─────────────────────────────────────────────────────────────────────
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS services_index (
              service_key TEXT PRIMARY KEY,
              host TEXT,
              first_ts TEXT,
              last_ts TEXT,
              exchange_count INTEGER NOT NULL DEFAULT 0,
              endpoint_count INTEGER NOT NULL DEFAULT 0,
              has_auth INTEGER NOT NULL DEFAULT 0,
              auth_score INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_services_index_last_ts ON services_index(last_ts);"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS endpoints_index (
              service_key TEXT NOT NULL,
              method TEXT NOT NULL,
              path_template TEXT NOT NULL,
              first_ts TEXT,
              last_ts TEXT,
              count INTEGER NOT NULL DEFAULT 0,
              success_count INTEGER NOT NULL DEFAULT 0,
              last_status INTEGER,
              example_exchange_id INTEGER,
              PRIMARY KEY(service_key, method, path_template)
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_endpoints_index_service_last_ts "
            "ON endpoints_index(service_key, last_ts);"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              service_key TEXT NOT NULL,
              ts TEXT NOT NULL,
              cookie_header TEXT,
              cookie_hash TEXT,
              headers_json TEXT,
              source_exchange_id INTEGER,
              FOREIGN KEY(source_exchange_id) REFERENCES exchanges(id) ON DELETE SET NULL
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_auth_snapshots_service_ts "
            "ON auth_snapshots(service_key, ts DESC);"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS replays (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              tag TEXT,
              original_exchange_id INTEGER NOT NULL,
              replay_exchange_id INTEGER NOT NULL,
              origin TEXT,
              set_headers_json TEXT,
              drop_headers_json TEXT,
              patch_json_json TEXT,
              notes_json TEXT,
              FOREIGN KEY(original_exchange_id) REFERENCES exchanges(id) ON DELETE CASCADE,
              FOREIGN KEY(replay_exchange_id) REFERENCES exchanges(id) ON DELETE CASCADE
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_replays_ts ON replays(ts);")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_replays_original ON replays(original_exchange_id);"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_replays_tag_ts ON replays(tag, ts DESC);")

        self._conn.commit()

    @staticmethod
    def _normalize_service_key(host: str | None, service_key: str | None) -> str:
        if service_key:
            return str(service_key)
        if host:
            return str(host).replace(".", "_").replace(":", "_")
        return "unknown"

    @staticmethod
    def _auth_header_subset(req_headers: Sequence[HeaderField]) -> dict[str, str]:
        """Extract a best-effort subset of auth-relevant headers.

        Note: This is intentionally heuristic. The goal is to keep a stable auth bundle
        for replay/chaining, not to preserve full-fidelity request headers.
        """
        out: dict[str, str] = {}
        for h in req_headers:
            name = str(h.name)
            lower = name.lower()
            if lower in {"authorization", "cookie"}:
                out[name] = str(h.value)
                continue
            if "csrf" in lower or "xsrf" in lower:
                out[name] = str(h.value)
                continue
            if "api-key" in lower or "apikey" in lower:
                out[name] = str(h.value)
                continue
        return out

    def _update_indexes_no_commit(
        self,
        cur: sqlite3.Cursor,
        *,
        exchange_id: int,
        ts: str,
        host: str | None,
        method: str,
        path: str | None,
        status: int | None,
        service_key: str | None,
        req_headers: Sequence[HeaderField],
    ) -> None:
        """Update derived/index tables for a newly inserted exchange."""
        skey = self._normalize_service_key(host, service_key)
        host_str = str(host) if host is not None else None
        is_success = 1 if (status is not None and 200 <= int(status) < 300) else 0

        # Service upsert + counters
        auth_subset = self._auth_header_subset(req_headers)
        has_auth = 1 if auth_subset else 0
        auth_score = len(auth_subset)

        cur.execute(
            """
            INSERT OR IGNORE INTO services_index(
              service_key, host, first_ts, last_ts,
              exchange_count, endpoint_count, has_auth, auth_score
            ) VALUES (?,?,?,?,0,0,0,0)
            """,
            (skey, host_str, ts, ts),
        )
        cur.execute(
            """
            UPDATE services_index
               SET host = COALESCE(host, ?),
                   last_ts = CASE WHEN last_ts IS NULL OR last_ts < ? THEN ? ELSE last_ts END,
                   exchange_count = exchange_count + 1,
                   has_auth = CASE WHEN has_auth < ? THEN ? ELSE has_auth END,
                   auth_score = CASE WHEN auth_score < ? THEN ? ELSE auth_score END
             WHERE service_key = ?
            """,
            (host_str, ts, ts, has_auth, has_auth, auth_score, auth_score, skey),
        )

        # Endpoint template upsert + counters
        path_template = templatize_path(path or "")
        cur.execute(
            """
            INSERT OR IGNORE INTO endpoints_index(
              service_key, method, path_template,
              first_ts, last_ts,
              count, success_count, last_status, example_exchange_id
            ) VALUES (?,?,?,?,?,0,0,NULL,NULL)
            """,
            (skey, method, path_template, ts, ts),
        )
        inserted_endpoint = cur.rowcount == 1
        if inserted_endpoint:
            cur.execute(
                "UPDATE services_index SET endpoint_count = endpoint_count + 1 "
                "WHERE service_key = ?",
                (skey,),
            )
        cur.execute(
            """
            UPDATE endpoints_index
               SET last_ts = CASE WHEN last_ts IS NULL OR last_ts < ? THEN ? ELSE last_ts END,
                   count = count + 1,
                   success_count = success_count + ?,
                   last_status = ?,
                   example_exchange_id = CASE
                     WHEN example_exchange_id IS NULL AND ? = 1 THEN ?
                     ELSE example_exchange_id
                   END
             WHERE service_key = ? AND method = ? AND path_template = ?
            """,
            (ts, ts, is_success, status, is_success, exchange_id, skey, method, path_template),
        )

        # Auth snapshot (best-effort): store on successful exchanges with auth-ish headers.
        if is_success and has_auth:
            cookie_header = None
            for k, v in auth_subset.items():
                if k.lower() == "cookie":
                    cookie_header = v
                    break

            cookie_hash = None
            canonical_cookie = None
            if isinstance(cookie_header, str) and cookie_header:
                cookies = parse_cookie_header(cookie_header)
                canonical_cookie = format_cookie_header(cookies) if cookies else cookie_header
                cookie_hash = hashlib.sha256(
                    canonical_cookie.encode("utf-8", errors="ignore")
                ).hexdigest()

            headers_json = json.dumps(auth_subset, ensure_ascii=False, separators=(",", ":"))

            # Deduplicate: skip insert if the latest snapshot has identical auth data.
            latest = cur.execute(
                "SELECT cookie_hash, headers_json FROM auth_snapshots "
                "WHERE service_key = ? ORDER BY ts DESC, id DESC LIMIT 1",
                (skey,),
            ).fetchone()
            is_dup = (
                latest is not None
                and latest["cookie_hash"] == cookie_hash
                and latest["headers_json"] == headers_json
            )
            if not is_dup:
                cur.execute(
                    """
                    INSERT INTO auth_snapshots(
                      service_key, ts, cookie_header, cookie_hash, headers_json, source_exchange_id
                    )
                    VALUES (?,?,?,?,?,?)
                    """,
                    (skey, ts, canonical_cookie, cookie_hash, headers_json, exchange_id),
                )

    def _truncate_body(self, body: bytes | None) -> tuple[bytes | None, int, bool]:
        if body is None:
            return None, 0, False
        if len(body) <= self.max_body_bytes:
            return body, len(body), False
        return body[: self.max_body_bytes], len(body), True

    def put_exchange(
        self,
        *,
        scheme: str | None,
        host: str | None,
        port: int | None,
        method: str,
        path: str | None,
        query: str | None,
        url: str | None,
        status: int | None,
        graphql_operation: str | None = None,
        endpoint: str | None = None,
        service_key: str | None = None,
        req_headers: Sequence[HeaderField] = (),
        resp_headers: Sequence[HeaderField] = (),
        req_body: bytes | None = None,
        resp_body: bytes | None = None,
        req_content_type: str | None = None,
        resp_content_type: str | None = None,
        ts: str | None = None,
    ) -> int:
        """Insert a captured exchange and return its id."""
        cur = self._conn.cursor()
        try:
            exchange_id = self._put_exchange_no_commit(
                cur,
                scheme=scheme,
                host=host,
                port=port,
                method=method,
                path=path,
                query=query,
                url=url,
                status=status,
                graphql_operation=graphql_operation,
                endpoint=endpoint,
                service_key=service_key,
                req_headers=req_headers,
                resp_headers=resp_headers,
                req_body=req_body,
                resp_body=resp_body,
                req_content_type=req_content_type,
                resp_content_type=resp_content_type,
                ts=ts,
            )
            self._conn.commit()
            return exchange_id
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def _put_exchange_no_commit(
        self,
        cur: sqlite3.Cursor,
        *,
        scheme: str | None,
        host: str | None,
        port: int | None,
        method: str,
        path: str | None,
        query: str | None,
        url: str | None,
        status: int | None,
        graphql_operation: str | None = None,
        endpoint: str | None = None,
        service_key: str | None = None,
        req_headers: Sequence[HeaderField] = (),
        resp_headers: Sequence[HeaderField] = (),
        req_body: bytes | None = None,
        resp_body: bytes | None = None,
        req_content_type: str | None = None,
        resp_content_type: str | None = None,
        ts: str | None = None,
    ) -> int:
        ts = ts or _utcnow_iso()
        req_raw, req_len, req_trunc = self._truncate_body(req_body)
        resp_raw, resp_len, resp_trunc = self._truncate_body(resp_body)

        # When body wasn't captured (e.g. streamed), use content-length header as best estimate.
        if resp_body is None and resp_len == 0 and resp_headers:
            for h in resp_headers:
                if h.name.lower() == "content-length":
                    try:
                        resp_len = int(h.value)
                    except (ValueError, TypeError):
                        pass
                    break

        cur.execute(
            """
            INSERT INTO exchanges (
              ts, scheme, host, port, method, path, query, url, status,
              graphql_operation, endpoint, service_key,
              req_content_type, resp_content_type,
              req_body_len, resp_body_len, req_body_sha256, resp_body_sha256,
              req_body_truncated, resp_body_truncated
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
            """,
            (
                ts,
                scheme,
                host,
                port,
                method,
                path,
                query,
                url,
                status,
                graphql_operation,
                endpoint,
                service_key,
                req_content_type,
                resp_content_type,
                req_len,
                resp_len,
                _sha256(req_body),
                _sha256(resp_body),
                1 if req_trunc else 0,
                1 if resp_trunc else 0,
            ),
        )
        if cur.lastrowid is None:  # pragma: no cover
            raise RuntimeError("SQLite insert did not return lastrowid")
        exchange_id = int(cur.lastrowid)

        if req_headers:
            cur.executemany(
                "INSERT INTO headers(exchange_id, side, idx, name, value) VALUES (?,?,?,?,?)",
                [(exchange_id, "request", i, h.name, h.value) for i, h in enumerate(req_headers)],
            )
        if resp_headers:
            cur.executemany(
                "INSERT INTO headers(exchange_id, side, idx, name, value) VALUES (?,?,?,?,?)",
                [(exchange_id, "response", i, h.name, h.value) for i, h in enumerate(resp_headers)],
            )

        cur.execute(
            "INSERT OR REPLACE INTO bodies(exchange_id, side, raw) VALUES (?,?,?)",
            (exchange_id, "request", req_raw),
        )
        cur.execute(
            "INSERT OR REPLACE INTO bodies(exchange_id, side, raw) VALUES (?,?,?)",
            (exchange_id, "response", resp_raw),
        )

        # Derived/index updates (same transaction).
        self._update_indexes_no_commit(
            cur,
            exchange_id=exchange_id,
            ts=ts,
            host=host,
            method=method,
            path=path,
            status=status,
            service_key=service_key,
            req_headers=req_headers,
        )

        return exchange_id

    def get_exchange(self, exchange_id: int) -> dict:
        """Fetch a single exchange with headers and bodies."""
        cur = self._conn.cursor()
        row = cur.execute("SELECT * FROM exchanges WHERE id = ?", (exchange_id,)).fetchone()
        if row is None:
            raise KeyError(exchange_id)

        headers_rows = cur.execute(
            "SELECT side, idx, name, value FROM headers WHERE exchange_id = ? ORDER BY side, idx",
            (exchange_id,),
        ).fetchall()
        bodies_rows = cur.execute(
            "SELECT side, raw FROM bodies WHERE exchange_id = ?",
            (exchange_id,),
        ).fetchall()

        headers: dict[str, list[dict[str, str]]] = {"request": [], "response": []}
        for r in headers_rows:
            headers[r["side"]].append({"name": r["name"], "value": r["value"]})

        bodies: dict[str, bytes | None] = {"request": None, "response": None}
        for r in bodies_rows:
            bodies[r["side"]] = r["raw"]

        return {
            **dict(row),
            "headers": headers,
            "bodies": bodies,
        }

    def search_exchanges(
        self,
        *,
        service_key: str | None = None,
        host: str | None = None,
        method: str | None = None,
        status: int | None = None,
        path_contains: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Search exchanges with simple filters."""
        clauses: list[str] = []
        params: list[object] = []

        if service_key:
            clauses.append("service_key = ?")
            params.append(service_key)
        if host:
            clauses.append("host = ?")
            params.append(host)
        if method:
            clauses.append("method = ?")
            params.append(method)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if path_contains:
            clauses.append("path LIKE ?")
            params.append(f"%{path_contains}%")

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        q = (
            "SELECT id, ts, scheme, host, port, method, path, query, url, status, "
            "resp_body_len, graphql_operation "
            "FROM exchanges"
            f"{where} ORDER BY (status BETWEEN 200 AND 299) DESC, id DESC LIMIT ? OFFSET ?"
        )
        params.append(limit)
        params.append(offset)

        cur = self._conn.cursor()
        rows = cur.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def discover_js_endpoints(
        self,
        *,
        host: str | None = None,
        service: str | None = None,
        limit_assets: int = 200,
        limit: int = 200,
        include: str | None = None,
        exclude: str | None = None,
    ) -> list[dict[str, object]]:
        """Extract endpoint/URL hints from captured JS bundles (LinkFinder-style).

        This is best-effort and intentionally heuristic: it produces *hints* for endpoints
        referenced by the frontend, even if not yet called.
        """
        skey = self.resolve_service_key(service) if service else None
        cur = self._conn.cursor()
        try:
            clauses: list[str] = [
                "b.side = 'response'",
                "b.raw IS NOT NULL",
                "("
                "COALESCE(e.resp_content_type,'') LIKE '%javascript%' OR "
                "COALESCE(e.resp_content_type,'') LIKE '%ecmascript%' OR "
                "COALESCE(e.path,'') LIKE '%.js' OR "
                "COALESCE(e.path,'') LIKE '%.mjs'"
                ")",
            ]
            params: list[object] = []
            if host:
                clauses.append("e.host = ?")
                params.append(host)
            if skey:
                clauses.append("(e.service_key = ? OR REPLACE(COALESCE(e.host,''),'.','_') = ?)")
                params.extend([skey, skey])

            where = " AND ".join(clauses)
            q = (
                "SELECT e.id, e.host, e.path, e.ts, b.raw "
                "FROM exchanges e "
                "JOIN bodies b ON b.exchange_id = e.id "
                f"WHERE {where} "
                "ORDER BY e.id DESC "
                "LIMIT ?"
            )
            params.append(int(limit_assets))
            rows = cur.execute(q, params).fetchall()

            counts: dict[str, dict[str, object]] = {}
            inc = include.lower() if include else None
            exc = exclude.lower() if exclude else None

            for r in rows:
                raw = r["raw"]
                if not isinstance(raw, (bytes, bytearray)) or not raw:
                    continue
                text = bytes(raw).decode("utf-8", errors="ignore")
                for ep in _extract_from_js_text(text):
                    low = ep.lower()
                    if inc and inc not in low:
                        continue
                    if exc and exc in low:
                        continue
                    slot = counts.get(ep)
                    if slot is None:
                        counts[ep] = {
                            "endpoint": ep,
                            "count": 1,
                            "example_js_exchange_id": int(r["id"]),
                            "example_js_path": r["path"],
                            "host": r["host"],
                            "last_seen": r["ts"],
                        }
                    else:
                        c = slot.get("count")
                        slot["count"] = (int(c) if isinstance(c, int) else 0) + 1

            def _count_key(d: dict[str, object]) -> int:
                c = d.get("count")
                return int(c) if isinstance(c, int) else 0

            out = sorted(counts.values(), key=_count_key, reverse=True)
            return out[: int(limit)]
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def linkfinder_js(
        self,
        *,
        host: str | None = None,
        service: str | None = None,
        limit_assets: int = 200,
        limit: int = 500,
        filter_regex: str | None = None,
        beautify: bool = False,
    ) -> list[dict[str, object]]:
        """Run LinkFinder-style extraction over captured JS responses.

        Returns a frequency-ranked list of dicts:
        - link: extracted endpoint/url/path
        - count: number of occurrences across scanned assets
        - example_js_exchange_id / example_js_path: where we saw it
        """
        from .linkfinder import extract_links

        skey = self.resolve_service_key(service) if service else None
        cur = self._conn.cursor()
        try:
            clauses: list[str] = [
                "b.side = 'response'",
                "b.raw IS NOT NULL",
                "("
                "COALESCE(e.resp_content_type,'') LIKE '%javascript%' OR "
                "COALESCE(e.resp_content_type,'') LIKE '%ecmascript%' OR "
                "COALESCE(e.path,'') LIKE '%.js' OR "
                "COALESCE(e.path,'') LIKE '%.mjs'"
                ")",
            ]
            params: list[object] = []
            if host:
                clauses.append("e.host = ?")
                params.append(host)
            if skey:
                clauses.append("(e.service_key = ? OR REPLACE(COALESCE(e.host,''),'.','_') = ?)")
                params.extend([skey, skey])

            where = " AND ".join(clauses)
            q = (
                "SELECT e.id, e.host, e.path, e.ts, b.raw "
                "FROM exchanges e "
                "JOIN bodies b ON b.exchange_id = e.id "
                f"WHERE {where} "
                "ORDER BY e.id DESC "
                "LIMIT ?"
            )
            params.append(int(limit_assets))
            rows = cur.execute(q, params).fetchall()

            counts: dict[str, dict[str, object]] = {}
            for r in rows:
                raw = r["raw"]
                if not isinstance(raw, (bytes, bytearray)) or not raw:
                    continue
                text = bytes(raw).decode("utf-8", errors="ignore")
                links = extract_links(
                    text, filter_regex=filter_regex, beautify=beautify, unique=False
                )
                for link in links:
                    slot = counts.get(link)
                    if slot is None:
                        counts[link] = {
                            "link": link,
                            "count": 1,
                            "example_js_exchange_id": int(r["id"]),
                            "example_js_path": r["path"],
                            "host": r["host"],
                            "last_seen": r["ts"],
                        }
                    else:
                        c = slot.get("count")
                        slot["count"] = (int(c) if isinstance(c, int) else 0) + 1

            def _count_key(d: dict[str, object]) -> int:
                c = d.get("count")
                return int(c) if isinstance(c, int) else 0

            out = sorted(counts.values(), key=_count_key, reverse=True)
            return out[: int(limit)]
        finally:
            try:
                cur.close()
            except Exception:
                pass
