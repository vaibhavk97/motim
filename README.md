# motim

**M**odel **O**ver **T**raffic — **I**ntercept & **M**anage

A search, inspect, and replay layer over network traffic for AI agents.

motim runs a local proxy that captures HTTP(S) traffic into a SQLite database. Agents query it via CLI to understand how web services work and operate on them — no API docs, no credential wiring, no guesswork.

```
Browser                              Agent
   │                                   │
   ▼                                   ▼
┌──────────┐    ┌────────────┐    ┌──────────────┐
│  motim   │───▶│  SQLite    │◀───│  search      │
│  proxy   │    │  (traffic) │    │  inspect     │
│  :8080   │    │            │    │  replay      │
└──────────┘    └────────────┘    └──────────────┘
```

You browse a site. motim records every request and response. Now your agent can search that traffic, understand the API, and replay requests — with the exact auth, headers, and body the browser used.

## Examples

**Agent discovers how an API works:**
```bash
motim endpoints --service example --json
motim search --host api.example.com --method POST --json
motim show 42 --json    # full request + response
```

**Agent replays a captured request:**
```bash
motim replay 42 --json
motim replay 42 --patch-json '{"page_size": 50}' --json
motim replay 42 --set-header "x-custom=value" --json
```

**Agent investigates a failure:**
```bash
motim search --host api.example.com --status 403 --json
motim diff 42 43 --json
motim around 42 --window 60 --json    # nearby exchanges
```

**Agent probes an endpoint:**
```bash
motim probe 42 \
  --patch-json '{"admin": true}' \
  --drop-header "authorization" \
  --json
```

**Agent extracts endpoints from JavaScript:**
```bash
motim linkfinder --host app.example.com --regex '^/api/' --json
```

Every command supports `--json` for machine-readable output.

## Install

```bash
pip install motim
```

Optional extras:
```bash
pip install 'motim[curl]'        # browser TLS fingerprinting
pip install 'motim[linkfinder]'  # JS endpoint extraction
```

## Getting started

```bash
motim init          # create dirs, trust CA cert, install agent skill
motim start         # start proxy on localhost:8080
# point your browser at localhost:8080 (use FoxyProxy or set system-wide)
# browse normally — traffic flows into ~/.motim/motim.sqlite3
```

## Agent integration

motim ships a skill file that teaches agents how to use it.

```bash
motim init              # auto-installs skill for Claude Code
motim agents-md         # writes AGENTS.md for Codex, opencode, etc.
```

The skill gives agents a decision tree: when to search, when to replay, when to probe, how to handle auth failures. Agents use the CLI with `--json` — no Python SDK needed.

## Use from any MCP-compatible agent

motim ships a native MCP server, so any Model Context Protocol client — Claude Desktop, Cursor, Cline, Continue, Zed, Goose, Windsurf, etc. — can drive motim without shelling out to the CLI.

```bash
pip install 'motim[mcp]'
motim mcp    # runs over stdio
```

Add to your MCP client config. Example for Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "motim": {
      "command": "motim",
      "args": ["mcp"]
    }
  }
}
```

The server exposes the same surface as the CLI as 13 typed tools:

| Tool | Maps to |
|---|---|
| `search_exchanges`, `show_exchange`, `cat_exchange` | `motim search/show/cat` |
| `list_endpoints`, `list_services` | `motim endpoints/services list` |
| `diff_exchanges`, `around`, `session` | `motim diff/around/session` |
| `linkfinder`, `proxy_status` | `motim linkfinder/status` |
| `replay`, `probe`, `replay_sequence` | `motim replay/probe/replay-seq` |

Read tools are marked `readOnlyHint=true` for friction-free auto-approval. `replay`, `probe`, and `replay_sequence` send real network requests and write results back to the DB — they're marked `destructiveHint=true` so clients can require explicit consent.

## CLI reference

```bash
# Proxy
motim start                     # start capture proxy
motim stop                      # stop proxy
motim status                    # check proxy status
motim doctor                    # health check

# Search & inspect
motim search [--host H] [--method M] [--status S] [--path-contains P]
motim show ID                   # full exchange
motim cat ID                    # response body only
motim cat ID --request          # request body only
motim export ID                 # as curl command
motim endpoints [--service S]   # endpoint patterns
motim services list              # captured services

# Replay & mutate
motim replay ID                 # replay as-is
motim replay ID --patch-json '...'           # patch JSON body
motim replay ID --set-header "k=v"           # add/override header
motim replay ID --drop-header "k"            # strip header
motim replay ID --transport curl --impersonate chrome  # TLS fingerprint

# Analyze
motim diff A B                  # diff two exchanges
motim probe ID [--patch-json ...] [--drop-header ...]  # mutation testing
motim around ID --window 60     # time-window slice
motim session ID                # session reconstruction
motim replay-seq ID1 ID2 ID3    # sequential replay

# JS analysis
motim linkfinder [--host H] [--regex R]
motim js-endpoints [--service S]

# Misc
motim export-yaml SERVICE       # YAML summary
motim rebuild-index             # rebuild derived indexes
motim config show               # view config
```

## Python library

```python
from motim import get, Client, ExchangeDB

# One-liner
r = get("api_example_com", "/v1/users/me")

# Client
client = Client("example")
r = client.get("/v1/users/me")

# Direct DB access
with ExchangeDB("~/.motim/motim.sqlite3") as db:
    results = db.search_exchanges(host="api.example.com", limit=10)
```

## Development

```bash
git clone https://github.com/vaibhavk97/motim.git
cd motim
pip install -e ".[dev]"
pytest
```

## License

MIT
