"""Unit tests for :mod:`evledger.paths` — the per-user default ledger root.

The default replaced the old ``<cwd>/ledger`` / ``<repo-root>/ledger`` behavior
(which splintered events per directory) with a stable XDG-style user data dir.
"""

from __future__ import annotations

from pathlib import Path

from evledger.paths import default_ledger_root


def test_default_uses_xdg_data_home_when_set(tmp_path: Path) -> None:
    out = default_ledger_root(env={"XDG_DATA_HOME": str(tmp_path)})
    assert out == tmp_path / "evledger" / "ledger"


def test_default_falls_back_to_local_share_when_xdg_unset() -> None:
    out = default_ledger_root(env={})
    assert out == Path.home() / ".local" / "share" / "evledger" / "ledger"


def test_default_treats_blank_xdg_as_unset() -> None:
    out = default_ledger_root(env={"XDG_DATA_HOME": "   "})
    assert out == Path.home() / ".local" / "share" / "evledger" / "ledger"


def test_default_is_not_cwd_relative(tmp_path: Path) -> None:
    # The whole point of the change: the default must not depend on cwd.
    out = default_ledger_root(env={"XDG_DATA_HOME": str(tmp_path)})
    assert out.is_absolute()
    assert "evledger" in out.parts and out.name == "ledger"
