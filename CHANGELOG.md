# Changelog

All notable changes to **evledger** are documented here. This project adheres
to [Semantic Versioning](https://semver.org/) (pre-1.0: minor bumps may carry
breaking changes, called out below).

## 0.2.0

### Changed (breaking)

- **Default ledger root is now a per-user data directory, not `<cwd>/ledger`.**
  When neither `--ledger-root`/`root` nor `$CLAUDE_LEDGER_ROOT` is set, the
  ledger root resolves to `$XDG_DATA_HOME/evledger/ledger` (else
  `~/.local/share/evledger/ledger`) via the new
  `evledger.paths.default_ledger_root`. Previously it defaulted to
  `<cwd>/ledger` (CLI: `<repo-root>/ledger`), which silently **splintered**
  events into many per-directory ledgers depending on where a tool ran — events
  written from a subdirectory never reached the ledger a sync or visualizer
  pointed at. Both the CLI (`evledger.cli.resolve_ledger_root`) and MCP
  (`evledger.mcp.tools.resolve_ledger_root`) resolvers share the new default.

  **Migration:** if you relied on the old per-directory default, pass
  `--ledger-root ./ledger` (or set `$CLAUDE_LEDGER_ROOT`) explicitly. To adopt
  the new behavior for existing data, move your `<dir>/ledger/<machine>/`
  partitions under `~/.local/share/evledger/ledger/`.

  The `--repo-root` CLI option and the `repo_root` / `cwd` resolver arguments
  are retained for backward compatibility but no longer affect resolution.

### Added

- `evledger.paths.default_ledger_root(env=None)` and `LEDGER_ROOT_ENV_VAR`,
  exported from the top-level `evledger` package.

## 0.1.1

- `serve --host` flag to expose the read-only web visualizer beyond localhost.

## 0.1.0

- Initial release: git-backed, append-only, machine-partitioned CloudEvents 1.0
  ledger (JSONL) — library, CLI, MCP server, and read-only web visualizer.
  Stdlib-only core.
