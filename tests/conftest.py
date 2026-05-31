"""Shared pytest fixtures for evledger tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_ledger_root(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the ledger root to a throwaway dir for every test.

    Anything that resolves the ledger root falls through to
    ``$CLAUDE_LEDGER_ROOT`` -> ``<cwd>/ledger``. Without this, running the suite
    from the repo root would write real events into the repo's own ``ledger/``.
    Tests needing a specific root override with ``monkeypatch.setenv`` (the later
    set wins).
    """
    monkeypatch.setenv("CLAUDE_LEDGER_ROOT", str(tmp_path_factory.mktemp("ledger")))
