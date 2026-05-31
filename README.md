# evledger

A git-backed, append-only, machine-partitioned **CloudEvents 1.0** event ledger
(JSONL). Pure-stdlib core; optional CLI, MCP server, and a read-only web
visualizer.

- **Append-only, never compress** — events are immutable; the ledger is built
  for long-lived storage.
- **Machine-partitioned** — `ledger/<machine>/<YYYY-MM>.jsonl`, with a monotonic
  per-machine `seq`.
- **Open data channel** — each event's `data` attribute is arbitrary JSON;
  optional per-type schemas validate advisorily.
- **Stdlib-only core** — the library and visualizer have zero runtime
  dependencies; the CLI adds `click`, the MCP server adds the `mcp` SDK.

> Full README (install, library / CLI / MCP / viz usage, examples) is filled in
> by the `docs-and-ci` task. This stub exists so the package builds.

## License

MIT
