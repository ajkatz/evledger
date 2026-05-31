"""Unit tests for :mod:`evledger.machine` — the machine-id resolver."""

from __future__ import annotations

import re
from pathlib import Path

from evledger import resolve_machine_id


def test_env_var_takes_precedence(tmp_path: Path) -> None:
    id_file = tmp_path / "machine-id"
    id_file.write_text("from-file\n", encoding="utf-8")

    result = resolve_machine_id(
        env={"CLAUDE_MACHINE_ID": "from-env"},
        id_file=id_file,
        hostname="from-host",
    )

    assert result == "from-env"


def test_env_var_is_stripped(tmp_path: Path) -> None:
    result = resolve_machine_id(
        env={"CLAUDE_MACHINE_ID": "  spaced  \n"},
        id_file=tmp_path / "nope",
        hostname="h",
    )

    assert result == "spaced"


def test_blank_env_var_is_ignored(tmp_path: Path) -> None:
    # A blank env var falls through to the next step (the id file), per the
    # documented resolution order env -> id-file -> hostname.
    id_file = tmp_path / "machine-id"
    result = resolve_machine_id(
        env={"CLAUDE_MACHINE_ID": "   "},
        id_file=id_file,
        hostname="from-host",
    )

    assert result != "from-host"
    assert re.fullmatch(r"[0-9a-f]{32}", result)
    assert id_file.read_text(encoding="utf-8").strip() == result


def test_reads_existing_id_file_when_no_env(tmp_path: Path) -> None:
    id_file = tmp_path / "machine-id"
    id_file.write_text("persisted-id", encoding="utf-8")

    result = resolve_machine_id(env={}, id_file=id_file, hostname="from-host")

    assert result == "persisted-id"


def test_creates_uuid4_file_when_absent(tmp_path: Path) -> None:
    id_file = tmp_path / "sub" / "machine-id"
    assert not id_file.exists()

    result = resolve_machine_id(env={}, id_file=id_file, hostname="from-host")

    # A fresh uuid4 hex was generated, persisted, and returned.
    assert re.fullmatch(r"[0-9a-f]{32}", result)
    assert id_file.exists()
    assert id_file.read_text(encoding="utf-8").strip() == result


def test_created_file_is_stable_across_calls(tmp_path: Path) -> None:
    id_file = tmp_path / "machine-id"

    first = resolve_machine_id(env={}, id_file=id_file, hostname="h")
    second = resolve_machine_id(env={}, id_file=id_file, hostname="h")

    assert first == second


def test_falls_back_to_hostname_when_file_unwritable(tmp_path: Path) -> None:
    # Point id_file at a path whose parent is a file, so mkdir/write fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    id_file = blocker / "machine-id"

    result = resolve_machine_id(env={}, id_file=id_file, hostname="fallback-host")

    assert result == "fallback-host"


def test_defaults_resolve_without_arguments() -> None:
    # Must not raise and must return a non-empty string using real defaults.
    result = resolve_machine_id()

    assert isinstance(result, str)
    assert result != ""
