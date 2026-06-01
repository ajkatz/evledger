"""Default on-disk location for the ledger.

Kept separate from the schema/store layer — which take an explicit ``root`` with
*no* default (Decision 9) — so the *policy* of "where does a ledger live when the
caller didn't say" lives in one place, shared by the CLI and MCP resolvers.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

#: Environment variable consulted for an explicit ledger instance root.
LEDGER_ROOT_ENV_VAR = "CLAUDE_LEDGER_ROOT"


def default_ledger_root(env: Mapping[str, str] | None = None) -> Path:
    """The per-user default ledger root when none is given explicitly or via env.

    Resolves to an XDG-style user data directory so every process on a machine
    shares **one** ledger regardless of the working directory it happens to run
    from::

        $XDG_DATA_HOME/evledger/ledger   (when XDG_DATA_HOME is set, non-blank)
        ~/.local/share/evledger/ledger   (otherwise)

    This replaces the old ``<cwd>/ledger`` / ``<repo-root>/ledger`` default,
    which silently splintered events into many per-directory ledgers depending
    on where a tool was invoked — so events written from a subdirectory never
    reached the ledger a sync/visualizer pointed at.

    Args:
        env: Environment mapping to read ``XDG_DATA_HOME`` from. Defaults to
            :data:`os.environ`.

    Returns:
        The resolved default ledger root (not necessarily existing — the store
        creates it on first append).
    """
    environ = os.environ if env is None else env
    xdg = environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg and xdg.strip() else Path.home() / ".local" / "share"
    return base / "evledger" / "ledger"
