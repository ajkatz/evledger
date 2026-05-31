"""Tests for the FastMCP ledger server adapter (``mcp-server`` task).

Two tiers:

* **SDK-free tier** (always runs): asserts the ledger core, the ``claude-kg``
  CLI, and even :mod:`evledger.mcp.server` itself import cleanly with
  the ``mcp`` SDK absent — the extra only gates *running* the server, never
  *importing* the package. Also source-scans ``server.py`` to prove no
  top-level ``mcp`` import sneaks in, and checks the missing-SDK launcher path
  emits an install hint instead of crashing.

* **SDK smoke tier** (``@requires_mcp`` skip-if-not-installed): when the
  ``[mcp]`` extra is installed, drives the real server through the SDK's
  in-memory client session and exercises all three tools end-to-end against a
  tmp ledger root. Skipped (suite stays green) when ``mcp`` is not installed —
  and crucially the SDK-free tier above is *not* gated, so the "core imports
  without the extra" guarantee is always asserted.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from evledger.mcp import server

#: Whether the optional ``mcp`` SDK is importable. Computed without importing it
#: (so the SDK-free tests in this module never trigger the import), and used to
#: skip only the smoke tier — the SDK-free tier above must always run.
_HAS_MCP = importlib.util.find_spec("mcp") is not None

requires_mcp = pytest.mark.skipif(
    not _HAS_MCP, reason="requires the optional [mcp] extra"
)


# --- SDK-free tier: core stays importable without the [mcp] extra ----------


def test_core_ledger_imports_without_mcp() -> None:
    """The public ledger API imports with no reference to the mcp SDK."""
    import evledger as ledger

    assert hasattr(ledger, "new_event")
    assert hasattr(ledger, "LedgerStore")


def test_cli_imports_without_mcp() -> None:
    """The evledger CLI module imports without pulling in the mcp SDK."""
    import evledger.cli as cli

    assert hasattr(cli, "ledger_group")


def test_server_module_imports_without_mcp() -> None:
    """Importing the server module itself must not require the mcp SDK."""
    # The module is already imported at the top of this file; reaching here
    # without an ImportError is the assertion. build_server/main exist.
    assert callable(server.build_server)
    assert callable(server.main)


def test_server_module_has_no_top_level_mcp_import() -> None:
    """Every ``mcp`` import in server.py must be lazy (inside a function).

    Top-level lines must not import the SDK. The only permitted module-scope
    reference is the ``TYPE_CHECKING`` guarded import (which never runs).
    """
    source = Path(server.__file__).read_text(encoding="utf-8")
    for raw in source.splitlines():
        line = raw.strip()
        if not line.startswith(("import ", "from ")):
            continue
        # Skip indented lines — those live inside functions / the TYPE_CHECKING
        # block and are therefore lazy (this loop only inspects column-0 lines).
        if raw[:1] in (" ", "\t"):
            continue
        assert "mcp.server" not in line and " mcp" not in f" {line}", line
        assert not line.startswith("import mcp"), line
        assert not line.startswith("from mcp"), line


def test_main_without_mcp_prints_hint(monkeypatch, capsys) -> None:
    """When build_server raises ModuleNotFoundError, main() exits 1 with a hint."""

    def _raise(root: object = None) -> None:
        raise ModuleNotFoundError("No module named 'mcp'")

    monkeypatch.setattr(server, "build_server", _raise)
    code = server.main(["--ledger-root", "/tmp/whatever"])
    assert code == 1
    captured = capsys.readouterr()
    assert "pip install" in captured.err
    assert "mcp" in captured.err


# --- SDK smoke tier: skipped unless the [mcp] extra is installed -----------


def _tool_payload(result: object) -> object:
    """Extract a tool's JSON return from a FastMCP CallToolResult.

    FastMCP serializes a non-string tool return as a JSON ``TextContent``. We
    parse the first text block, which is stable across SDK versions that may or
    may not also populate ``structuredContent``.
    """
    content = result.content  # type: ignore[attr-defined]
    text = content[0].text  # type: ignore[index,attr-defined]
    return json.loads(text)


def _run(async_fn: object) -> object:
    """Run a zero-arg async function on the asyncio backend.

    The smoke tests are async (the SDK's in-memory client is async-context), but
    this repo doesn't configure the ``pytest.mark.anyio`` plugin. Driving the
    coroutine function with ``anyio.run`` keeps the test bodies plain sync
    functions and avoids a plugin dependency. ``mcp`` already depends on
    ``anyio``, so it is importable whenever this tier runs.
    """
    import anyio

    return anyio.run(async_fn)  # type: ignore[arg-type]


@requires_mcp
def test_smoke_lists_three_tools(tmp_path: Path) -> None:
    """The server advertises exactly the three ledger tools."""
    from mcp.shared.memory import create_connected_server_and_client_session

    async def scenario() -> set[str]:
        app = server.build_server(root=tmp_path)
        # FastMCP exposes the low-level Server as ._mcp_server across SDK versions.
        async with create_connected_server_and_client_session(
            app._mcp_server
        ) as session:
            listed = await session.list_tools()
            return {t.name for t in listed.tools}

    names = _run(scenario)
    assert names == {"ledger_log_tool", "ledger_query_tool", "ledger_stats_tool"}


@requires_mcp
def test_smoke_log_then_query_then_stats(tmp_path: Path) -> None:
    """Round-trip: log an event via the tool, then query and stats it back."""
    from mcp.shared.memory import create_connected_server_and_client_session

    async def scenario() -> tuple[dict, dict, dict]:
        app = server.build_server(root=tmp_path)
        async with create_connected_server_and_client_session(
            app._mcp_server
        ) as session:
            logged = _tool_payload(
                await session.call_tool(
                    "ledger_log_tool",
                    {
                        "source": "/m1/dev",
                        "type": "dev.x.thing",
                        "machine": "m1",
                        "data": {"detail": "hi"},
                    },
                )
            )
            queried = _tool_payload(
                await session.call_tool("ledger_query_tool", {"machine": "m1"})
            )
            stats = _tool_payload(await session.call_tool("ledger_stats_tool", {}))
            return logged, queried, stats

    logged, queried, stats = _run(scenario)

    assert logged["source"] == "/m1/dev"
    assert logged["seq"] == 0
    assert logged["data"] == {"detail": "hi"}

    assert queried["count"] == 1
    assert queried["events"][0]["id"] == logged["id"]

    assert stats["total"] == 1
    assert stats["counts"] == {"dev.x.thing": 1}
