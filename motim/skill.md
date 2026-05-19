---
name: motim
description: Search, inspect, and replay network traffic. Use when the user asks to interact with web APIs, understand how a service works, debug API failures, or replay authenticated requests. Also use when seeing 401/403 errors.
---

# motim — network traffic layer for agents

motim captures browser HTTP(S) traffic into a local SQLite DB (`~/.motim/motim.sqlite3`). You search, inspect, replay, mutate, and diff those exchanges via CLI. The captured traffic is your knowledge of how the service works — no API docs needed.

**Always use `--json` for structured output.**

**If MCP tools are available** (their names match `search_exchanges`, `show_exchange`, `cat_exchange`, `list_endpoints`, `list_services`, `diff_exchanges`, `around`, `session`, `linkfinder`, `proxy_status`, `replay`, `probe`, `replay_sequence`), **prefer them** over shelling out to the CLI: they return typed JSON directly and avoid subprocess overhead. The decision tree below applies identically — CLI commands and MCP tool names map 1:1 (`motim search …` ↔ `search_exchanges`, `motim show ID` ↔ `show_exchange`, etc.).

## Decision tree

1. **Need to understand how a service works?** → `motim endpoints`, `motim search`, `motim show`
2. **Need to call an API the user has browsed?** → Find a matching exchange, then `motim replay`
3. **Need to test how an API responds to changes?** → `motim probe`
4. **Need to debug a failure?** → `motim search --status 403`, `motim diff`, `motim around`
5. **Need to discover endpoints?** → `motim endpoints` (observed) or `motim linkfinder` (from JS)
6. **Getting 401/403 on replay?** → Auth may be expired. Ask the user to browse the site again.
7. **Getting blocked by bot detection?** → Try `--transport curl --impersonate chrome`

## What gets captured

motim captures all HTTP(S) traffic except noise. It automatically filters out:
- **Analytics/tracking domains**: Google Analytics, Segment, Mixpanel, Amplitude, Hotjar, Sentry, DataDog, LaunchDarkly, etc.
- **Ad networks**: DoubleClick, Facebook, LinkedIn tracking pixels
- **Static assets**: images (png/jpg/svg), fonts (woff/ttf), media (mp4/webm), favicon.ico, robots.txt

Everything else is captured with full fidelity: method, URL, all headers, request body, response body, status code, timestamps.

## Service keys

Hostnames are normalized into service keys: `api.example.com` → `api_example_com`. You can use any format — `api.example.com`, `api_example_com`, or just `example` (partial match). All resolve to the same service.

## Path templates

Paths are templatized: UUIDs and numeric IDs are replaced with `{id}`. So `/v1/databases/abc-123/query` becomes `/v1/databases/{id}/query`. This lets you see endpoint patterns instead of individual requests.

## Header profiles

When replaying via the Python `Client`, header profiles control which captured headers are included:

| Profile | What's included |
|---------|----------------|
| `minimal` | Just `Authorization`, `X-API-Key`, `API-Key` |
| `standard` | Auth + cookies + `x-*` headers (excluding `x-request-id`, `x-correlation-id`) |
| `full` | All captured headers (excluding `host`, `content-length`, `connection`, `accept-encoding`) |

CLI `motim replay` always uses full headers from the original exchange. Use `--drop-header` to strip specific ones.

## GraphQL support

motim extracts GraphQL operation names from captured queries/mutations. Search results and endpoint summaries show the operation name (e.g., `GetUser`, `CreatePost`). Use `--path-contains "/graphql"` and inspect the body to find specific operations.

## WebSocket support

WebSocket messages are captured with direction tracking (send/recv). They appear in the exchange DB alongside regular HTTP exchanges.

## Commands

### Search exchanges

Find captured exchanges by host, method, status, path, or service.

```bash
motim search --host api.example.com --method POST --status 200 --json
motim search --service example --path-contains "/query" --limit 50 --json
motim search --service example --offset 100 --limit 50 --json  # pagination
```

### Inspect an exchange

View full request + response details for a specific exchange.

```bash
motim show 42 --json              # full request + response as JSON
motim show 42 --request-only      # request only
motim show 42 --response-only     # response only
motim show 42 --raw               # skip pretty-printing
motim cat 42                      # response body only (pipe-friendly)
motim cat 42 --request            # request body only
motim export 42                   # export as runnable curl command
```

### List endpoints

Show discovered endpoint patterns (method + templatized path) with hit counts and success rates.

```bash
motim endpoints --service example --json
motim endpoints --method POST --path-contains "/query" --json
```

### List services

Show all captured services with exchange counts.

```bash
motim services list --json             # list all services
motim services show example --json     # detailed service info
motim services auth example --json     # auth header snapshot
```

### Replay an exchange

Re-send a captured request exactly as-is (same URL, headers, body, auth) and store the result back in the DB.

```bash
motim replay 42 --json
```

### Replay with mutations

Modify the request before replaying. The original exchange is not modified.

```bash
motim replay 42 --set-header "x-custom=value" --json          # add/override a header
motim replay 42 --drop-header "x-csrf-token" --json            # remove a header
motim replay 42 --patch-json "{\"page_size\":50}" --json       # JSON merge patch on body
motim replay 42 --patch-file patch.json --json                 # patch from file
echo '{"key":"val"}' | motim replay 42 --patch-stdin --json    # patch from stdin
motim replay 42 --origin "https://other.example.com" --json    # change target host
motim replay 42 --body-file new_body.json --json               # replace entire body
```

### Bot detection bypass

Some sites block replayed requests based on TLS fingerprinting. Use the curl transport with browser impersonation.

```bash
# Requires: pip install 'motim[curl]'
motim replay 42 --transport curl --impersonate chrome --json
```

### Diff two exchanges

Compare two exchanges side by side — useful for understanding what changed between a working and broken request.

```bash
motim diff 42 43 --json
```

### Probe (automated mutation testing)

Replay a baseline, then run multiple mutations and diff each against the baseline. Each `--patch-json` and `--drop-header` is a separate run.

```bash
motim probe 42 \
  --patch-json "{\"admin\":true}" \
  --patch-json "{\"role\":\"superuser\"}" \
  --drop-header "authorization" \
  --drop-header "cookie" \
  --json
```

### Sequential replay

Replay multiple exchanges in order. Useful for multi-step flows (login → action → verify).

```bash
motim replay-seq 10 11 12 13 --json
```

### Time-window exploration

Find exchanges around a specific exchange in time. Useful for understanding what happened before/after a request.

```bash
motim around 42 --window 60 --json        # exchanges ±60 seconds
motim around 42 --window 60 --service example --json  # filter by service
```

### Session reconstruction

Extract a full session slice around an exchange using gap-based splitting. Automatically filters out noise (analytics, static assets).

```bash
motim session 42 --json                    # default 120s gap
motim session 42 --gap 300 --json          # 5-minute gap threshold
motim session 42 --no-filter-noise --json  # include everything
```

### JS endpoint extraction

Extract API routes from captured JavaScript bundles. Discovers endpoints referenced in frontend code even before you trigger them in the UI.

```bash
motim linkfinder --host app.example.com --json                    # all links
motim linkfinder --host app.example.com --regex '^/api/' --json   # filter by pattern
motim js-endpoints --service example --json                       # aggregated hints
```

Requires `pip install 'motim[linkfinder]'` for JS beautification support.

### YAML export

Export a lightweight YAML summary of a service (endpoints, auth snapshot) from the DB.

```bash
motim export-yaml myservice                   # writes to ~/.motim/exports/
motim export-yaml myservice --out ./myservice.yaml
```

### Rebuild indexes

If `motim services` or `motim endpoints` look empty after capturing traffic, rebuild derived indexes from raw exchanges.

```bash
motim rebuild-index --json
```

### Health check

Verify motim is set up correctly — checks directories, config, certificate, skill installation, and dependencies.

```bash
motim doctor --json
```

### Config

View or edit the configuration file (`~/.motim/config.yaml`).

```bash
motim config show      # print current config
motim config path      # print config file path
motim config edit      # open in $EDITOR
```

## Shell escaping

Always use double quotes with escaped inner quotes for `--patch-json`:

```bash
# Correct
motim replay 42 --patch-json "{\"field\":\"value\"}" --json

# Wrong — single quotes break in some agent harnesses
motim replay 42 --patch-json '{"field":"value"}' --json
```

## When auth fails

If replay returns 401/403, the captured session likely expired. Ask the user:

> "Your session for [service] seems expired. Could you open [service URL] in your browser with the motim proxy running and log in again?"

Then search for fresh exchanges and replay those instead.

## SPA gotcha

If you replay an `/api/...` request and get `200 text/html` back, you likely hit an SPA fallback page (wrong endpoint). Use `motim linkfinder` to discover the correct endpoint path, then capture the real UI action.

## Strategies (learned from real investigations)

### DB replay vs Client for fragile APIs

For stable public APIs, the Python `Client` works fine. For fragile/private APIs (undocumented endpoints, internal APIs), always prefer DB replay — it preserves every header and body byte exactly as the browser sent them. The `Client` reconstructs requests from config and may miss critical headers.

### Stale request-specific headers

Some APIs generate per-request headers that become stale on replay. If replay returns 404 or 403 unexpectedly, try dropping suspicious headers:

```bash
motim replay 42 --drop-header "x-client-transaction-id" --json
motim replay 42 --drop-header "x-request-id" --json
```

Some APIs (e.g., certain social platforms) 404 if you include stale per-request transaction IDs.

### Finding constant vs varying parameters

When multiple captures of the same endpoint exist, compare them to find which parameters are constant (required) vs varying (dynamic):

```python
from motim import Service
svc = Service.load("example")
comparison = svc.samples.compare("POST /api/endpoint")
print("Constant params:", comparison.constant_params)  # always the same
print("Varying params:", comparison.varying_params)     # change per request
```

### Auth expiry detection

Before replaying, check if auth might be expired:

```python
from motim import Service
svc = Service.load("example")
if svc.auth.is_expired:  # checks JWT exp claim if applicable
    # ask user to re-browse
    pass
print(svc.auth.type)       # "bearer", "cookie", "api_key"
print(svc.auth.last_seen)  # when credentials were last captured
```

### Proxy performance

If capture feels slow, the YAML spec writer is usually the bottleneck. For agent-first usage, switch to DB-only mode in `~/.motim/config.yaml`:

```yaml
capture:
  write_specs: false
```

### Identifying the right exchange to replay

Don't just replay the first match. Use these strategies:
1. Search with `--status 200` to find successful exchanges
2. Check `motim endpoints` for hit counts — high-count endpoints are more reliable
3. Use `motim show ID --json` to verify the response body looks right before replaying
4. Use `motim around ID --json` to see the surrounding context (was this part of a login flow?)

### Dealing with CSRF tokens

Many APIs require CSRF tokens that rotate. Strategy:
1. `motim session ID --json` to find the session flow
2. Find the exchange that sets the CSRF token (often in a response header or body)
3. `motim cat <csrf-exchange> --json` to extract the token
4. `motim replay <target> --set-header "x-csrf-token=<extracted>" --json`

## Python library

For programmatic access without CLI:

```python
from motim import get, Client, ExchangeDB

# One-liner with captured auth
r = get("api_example_com", "/v1/users/me")

# Client with connection pooling
with Client("example", auth_profile="full") as client:
    r = client.get("/v1/users/me")

# Direct DB queries
with ExchangeDB("~/.motim/motim.sqlite3") as db:
    results = db.search_exchanges(host="api.example.com", limit=10)
    ex = db.get_exchange(42)
```
