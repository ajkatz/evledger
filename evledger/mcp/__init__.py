"""MCP-facing surface for the event ledger (the ``ledger-mcp`` whim).

This subpackage exposes the ledger's ``log`` / ``query`` / ``stats`` operations
to MCP clients. It is split into two layers so the testable core stays free of
the MCP SDK:

* :mod:`~evledger.mcp.tools` — **SDK-free** handler functions
  (:func:`~evledger.mcp.tools.ledger_log`,
  :func:`~evledger.mcp.tools.ledger_query`,
  :func:`~evledger.mcp.tools.ledger_stats`) plus the ledger-root
  resolver (:func:`~evledger.mcp.tools.resolve_ledger_root`). These
  import only the public :mod:`evledger` API and the standard library,
  so they are unit-testable without ``mcp`` installed. **This is the layer the
  current task ships.**
* The FastMCP stdio server + ``claude-kg-ledger-mcp`` launcher (the next task)
  will bind these handlers as tools. That layer — and only that layer — imports
  the optional ``mcp`` SDK, kept behind the ``[mcp]`` extra and a lazy import so
  the ledger core and the ``claude-kg`` CLI never depend on it.

Importing *this* module is safe without the ``mcp`` extra: it re-exports only
the SDK-free handlers.
"""

from __future__ import annotations

from evledger.mcp.tools import (
    ledger_log,
    ledger_query,
    ledger_stats,
    resolve_ledger_root,
)

__all__ = [
    "resolve_ledger_root",
    "ledger_log",
    "ledger_query",
    "ledger_stats",
]
