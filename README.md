# evledger

A git-backed, append-only, machine-partitioned **CloudEvents 1.0** event ledger
(JSONL). Pure-stdlib core; optional CLI, MCP server, and a zero-build web
visualizer.

- **Append-only, never compress** — events are immutable; the ledger is built
  for long-lived storage and replay.
- **Machine-partitioned** — events live at `<root>/<machine>/<YYYY-MM>.jsonl`,
  with a monotonic per-machine `seq` assigned at append under a file lock.
- **CloudEvents 1.0 envelope** — every record is a valid CloudEvent
  (`specversion`/`id`/`source`/`type`/`time` + `machine`/`seq`); the `data`
  attribute is an open channel for arbitrary JSON.
- **Optional per-type schemas** — register a `data` schema and validation is
  *advisory* (a slightly-off payload is still logged, never dropped).
- **Stdlib-only core** — the library and the visualizer have **zero** runtime
  dependencies. The CLI adds `click`; the MCP server adds the `mcp` SDK; both
  are optional extras.

## Install

```bash
pip install evledger                # core library (stdlib only) + the `evledger` CLI*
pip install "evledger[mcp]"         # + the MCP server (`evledger-mcp`)
pip install "evledger[dev]"         # + pytest / ruff / mypy for development
```

\* the `evledger` console script needs `click`; `pip install "evledger[cli]"`
pulls it explicitly (it's also included by `[dev]`).

## Library

The core is pure functions over an immutable event stream — easy to test, no
hidden I/O beyond the store.

```python
from evledger import LedgerStore, Query, new_event, query_events
from evledger.stats import counts_by_type, pair_events

store = LedgerStore(root="./ledger")

# Append — `seq` is assigned per-machine, monotonic, under a lock.
store.append(new_event(
    source="/laptop/app", type="com.example.job.start",
    machine="laptop", time="2026-05-30T10:00:00Z", data={"job": "sync"},
))

events = store.read_all().events                       # ordered by (time, seq)
starts = query_events(events, Query(type="com.example.job.*"))   # glob match
counts = counts_by_type(events)                        # {type: n}
durations = pair_events(events).durations              # pair *.start with *.end
```

See [`examples/quickstart.py`](examples/quickstart.py) for a runnable end-to-end
walkthrough (`python examples/quickstart.py`).

### Layers

| Module | What it gives you |
|---|---|
| `evledger.schema` | `LedgerEvent`, `new_event(...)`, `parse_time(...)` — the CloudEvents envelope |
| `evledger.store` | `LedgerStore` — append (locked, `seq`-assigning) + `read_all()` / `iter_events()` |
| `evledger.query` | `Query` + `query_events(...)` — filter by source/type(glob)/machine/time window |
| `evledger.stats` | `counts_by_type`, `event_rate`, `pair_events` (→ `*.start`/`*.end` durations) |
| `evledger.registry` | `TypeRegistry` — register per-type `data` schemas, validate advisorily |
| `evledger.digest` | deterministic rollup + rule-based anomaly flags over a window |
| `evledger.transcript` / `evledger.reconstruct` | optional model-assisted audit layers (stdlib loader + injectable model client) |

## CLI

The `evledger` command exposes the ledger over a small group:

```bash
evledger log --source /laptop/app --type com.example.job.start \
  --machine laptop --data '{"job":"sync"}'

evledger show --type 'com.example.*' --since 2026-05-01 --limit 50
evledger stats --by type --pair          # counts + paired *.start/*.end durations
evledger digest --since 2026-05-01        # deterministic rollup + anomaly flags
evledger serve                            # read-only web visualizer (localhost)
evledger serve --host 0.0.0.0 --no-open   # expose the (read-only) dashboard on the LAN/Tailscale
```

The ledger root resolves via `--ledger-root` → `$CLAUDE_LEDGER_ROOT` → the
per-user default `$XDG_DATA_HOME/evledger/ledger` (else
`~/.local/share/evledger/ledger`). The default is a stable per-user location,
**not** `<cwd>/ledger` — so a tool run from any directory writes to the one
ledger instead of splintering events into per-directory ones. Point several
machines at one root (or sync each machine's partition to a master) to
aggregate.

## MCP server

The same `log` / `query` / `stats` operations are exposed to any MCP client
(Claude Desktop, other agents) over stdio. The MCP SDK is imported lazily, so
the core and CLI stay dependency-free.

```bash
# zero-install via uvx:
uvx --from "evledger[mcp]" evledger-mcp --ledger-root ~/.local/share/evledger

# or installed:
pipx install "evledger[mcp]"
evledger-mcp --ledger-root ~/.local/share/evledger
```

The ledger root is **server-side configuration** (captured at launch via
`--ledger-root` → `$CLAUDE_LEDGER_ROOT` → the per-user
`~/.local/share/evledger/ledger`), not a tool argument.

## Web visualizer

`evledger serve` starts a **read-only**, zero-build single-page app (stdlib
`http.server`, no framework) bound to localhost: a filter panel, a
wheel-zoom / drag-pan canvas timeline laned by source or type, a zoomable flame
graph of derived `*.start`/`*.end` spans, and a sortable event table. JSON APIs
(`/api/events`, `/api/spans`, `/api/meta`) back it. It never writes the ledger.

## Storage layout

```
<root>/
  <machine-a>/
    2026-05.jsonl     # one CloudEvent per line, append-only
    2026-06.jsonl
  <machine-b>/
    2026-05.jsonl
```

Each line is a complete CloudEvent. Files are never rewritten or compressed —
roll-up/aggregation happens at read time, so the raw record is preserved for the
life of the ledger.

## Development

```bash
pip install -e ".[dev,mcp]"
ruff check .
mypy evledger
pytest -q
```

CI (GitHub Actions) runs ruff + mypy + pytest across Python 3.10–3.13.

## License

MIT — see [LICENSE](LICENSE).
