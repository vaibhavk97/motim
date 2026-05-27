# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **Native MCP server** (`motim mcp`). Exposes search, inspect, replay,
  diff, and probe primitives as Model Context Protocol tools so any
  MCP-compatible agent (Claude Desktop, Cursor, Cline, Continue, Zed,
  Goose, Windsurf, etc.) can drive motim without shelling out. Read
  tools are marked `readOnlyHint`; `replay`/`probe`/`replay_sequence`
  are marked `destructiveHint`.
- Optional dependency: `pip install 'motim[mcp]'`.
- `motim init` now prints a Claude Desktop config snippet when the MCP
  extra is installed.
- `motim doctor` reports whether the `mcp` package is available
  (informational; does not affect the overall health verdict).

## [0.2.0] - 2025-02-15

### Added
- `show`, `cat`, `export` commands for inspecting individual exchanges
- `--json` flag for machine-readable output across commands
- `--patch-file` and `--patch-stdin` flags for replay body mutations
- Flexible service name resolution (e.g. `example` matches `api_example_com`)
- Context manager support for `Client` and `AsyncClient`
- `service_key` index for faster service lookups
- Auth deduplication (avoids storing duplicate credentials)
- `--offset` flag for paginated search results
- `probe` command auto-diff (compares mutations against baseline)

### Fixed
- Missing body capture for certain response types
- Improved UX when body is unavailable (clear messaging instead of empty output)

## [0.1.0] - 2025-01-06

### Added
- Initial release
- MITM proxy capture via mitmproxy addon
- SQLite exchange database with full-fidelity storage
- `search`, `replay`, `diff`, `endpoints`, `services` CLI commands
- `replay-seq` for sequential multi-request replay
- `around` and `session` for time-based exchange exploration
- `linkfinder` and `js-endpoints` for JS bundle scanning
- `export-yaml` for lightweight YAML summaries
- Authentication extraction and injection (headers, cookies, JWT)
- WebSocket message capture with direction tracking
- GraphQL operation name extraction
- Path templatization (UUIDs, numeric IDs replaced with `{id}`)
- Browser TLS fingerprinting via optional `curl_cffi` backend
- Agent skill file for agent integration
- Configurable header profiles (minimal, standard, full)
- Async buffered writes for proxy performance
