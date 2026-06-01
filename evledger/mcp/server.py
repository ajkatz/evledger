"""FastMCP stdio server for the event ledger (the ``mcp-server`` task).

This module is the **SDK-bound adapter layer**: it binds the SDK-free handlers
from :mod:`evledger.mcp.tools` (``ledger_log`` / ``ledger_query`` /
``ledger_stats``) as MCP tools on a `FastMCP` server and exposes a console-script
``main`` that resolves the ledger root and serves over stdio.

It is the **only** module in the package that imports the optional ``mcp`` SDK,
and it does so **lazily** — every ``mcp`` import lives inside a function body,
never at module top level. That keeps the ledger core and the ``claude-kg`` CLI
importable when the ``[mcp]`` extra is not installed: importing *this* module is
cheap and SDK-free; the SDK is only pulled in when :func:`build_server` (or
:func:`main`) actually runs. A dedicated test asserts the module source carries
no top-level ``mcp`` import.

The three tools are registered against a fixed ledger root captured at launch
(``--ledger-root`` flag → ``$EVLEDGER_ROOT`` → ``~/.local/share/evledger/ledger``, via
:func:`~evledger.mcp.tools.resolve_ledger_root`). MCP clients pass only
the per-call tool arguments; the root is server-side configuration, not a tool
parameter. Tool return values are the plain JSON-serializable dicts/lists the
handlers already produce, so FastMCP serializes them straight back to the client.

Conventions: frozen dataclasses, ``from __future__ import annotations``,
``X | None``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evledger.mcp.tools import (
    ledger_log,
    ledger_query,
    ledger_stats,
    resolve_ledger_root,
)

if TYPE_CHECKING:  # pragma: no cover - type-checking only, no runtime import
    from mcp.server.fastmcp import FastMCP

#: The MCP server name advertised to clients during initialization.
SERVER_NAME = "claude-kg-ledger"


def build_server(root: Path | str | None = None) -> FastMCP:
    """Build a `FastMCP` server with the three ledger tools bound to ``root``.

    The ``mcp`` SDK is imported **here**, lazily, so that importing this module
    never requires the ``[mcp]`` extra. The given ledger ``root`` is captured by
    closure into each tool, so MCP clients never pass a root — they call the
    tools with only the documented per-operation arguments.

    Args:
        root: The ledger instance root to bind every tool to. ``None`` resolves
            via :func:`~evledger.mcp.tools.resolve_ledger_root`
            (``$EVLEDGER_ROOT`` → ``~/.local/share/evledger/ledger``).

    Returns:
        A configured `FastMCP` instance, ready for ``.run()``.

    Raises:
        ModuleNotFoundError: if the ``mcp`` SDK is not installed (install the
            ``[mcp]`` extra: ``pip install claude-kg[mcp]``).
    """
    from mcp.server.fastmcp import FastMCP

    resolved_root = resolve_ledger_root(root)
    app: FastMCP = FastMCP(SERVER_NAME)

    @app.tool()
    def ledger_log_tool(
        source: str,
        type: str,
        data: Any | None = None,
        machine: str | None = None,
    ) -> dict[str, Any]:
        """Append one CloudEvent to the ledger; returns the stored event.

        Args:
            source: CloudEvents ``source`` URI-reference (e.g. ``/<machine>/<sys>``).
            type: Reverse-DNS event ``type`` (e.g. ``dev.example.mission.start``).
            data: Optional JSON payload — an object, or a JSON-encoded string
                (a non-JSON string is stored verbatim). Omit for no payload.
            machine: Machine partition key; omit to resolve from the environment.
        """
        return ledger_log(source=source, type=type, data=data, machine=machine,
                          root=resolved_root)

    @app.tool()
    def ledger_query_tool(
        source: str | None = None,
        type: str | None = None,
        machine: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Query the ledger; returns ``{count, events}`` ordered by ``(time, seq)``.

        Args:
            source: Exact ``source`` filter, or omit.
            type: ``type`` glob filter (e.g. ``dev.x.mission.*``), or omit.
            machine: Exact machine filter, or omit.
            since: Inclusive lower ISO-8601 time bound, or omit.
            until: Exclusive upper ISO-8601 time bound, or omit.
            limit: Keep only the most-recent ``limit`` events; ``0`` returns none.
        """
        return ledger_query(source=source, type=type, machine=machine, since=since,
                           until=until, limit=limit, root=resolved_root)

    @app.tool()
    def ledger_stats_tool(
        source: str | None = None,
        type: str | None = None,
        machine: str | None = None,
        since: str | None = None,
        until: str | None = None,
        by: str = "type",
        rate_unit: str = "hour",
        pair: bool = False,
    ) -> dict[str, Any]:
        """Descriptive aggregations over matching events: counts, rate, durations.

        Args:
            source: Exact ``source`` filter, or omit.
            type: ``type`` glob filter, or omit.
            machine: Exact machine filter, or omit.
            since: Inclusive lower ISO-8601 bound (also bounds the rate window).
            until: Exclusive upper ISO-8601 bound (also bounds the rate window).
            by: Counts breakdown key — ``"type"`` (default) or ``"source"``.
            rate_unit: Rate denominator — ``second`` / ``minute`` / ``hour`` / ``day``.
            pair: When true, also summarize paired ``*.start`` / ``*.end`` durations.
        """
        return ledger_stats(source=source, type=type, machine=machine, since=since,
                           until=until, by=by, rate_unit=rate_unit, pair=pair,
                           root=resolved_root)

    return app


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point: resolve the ledger root and serve over stdio.

    Parses ``--ledger-root`` (falling back to ``$EVLEDGER_ROOT`` →
    ``~/.local/share/evledger/ledger`` inside :func:`build_server`), builds the FastMCP server, and
    runs it on the stdio transport — the form Claude Desktop and ``uvx``/``pipx``
    launchers expect.

    Args:
        argv: Argument list (excluding the program name). ``None`` uses
            :data:`sys.argv`.

    Returns:
        Process exit code (``0`` on a clean shutdown). If the ``mcp`` SDK is
        missing, prints an install hint to stderr and returns ``1``.
    """
    parser = argparse.ArgumentParser(
        prog="claude-kg-ledger-mcp",
        description=(
            "Serve the claude-kg event ledger over MCP (stdio). Exposes "
            "ledger_log / ledger_query / ledger_stats to any MCP client."
        ),
    )
    parser.add_argument(
        "--ledger-root",
        default=None,
        help=(
            "Ledger instance root. Defaults to $EVLEDGER_ROOT, else "
            "~/.local/share/evledger/ledger."
        ),
    )
    args = parser.parse_args(argv)

    try:
        app = build_server(root=args.ledger_root)
    except ModuleNotFoundError:
        print(
            "The 'mcp' SDK is required to run the ledger MCP server. "
            "Install the optional extra:\n\n    pip install claude-kg[mcp]\n",
            file=sys.stderr,
        )
        return 1

    app.run(transport="stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry shim
    raise SystemExit(main())
