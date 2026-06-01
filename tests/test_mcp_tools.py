"""Unit tests for the SDK-free ledger MCP tool handlers (``mcp-tools`` task).

Exercises the handlers in :mod:`evledger.mcp.tools` directly against a
temporary ledger root — no ``mcp`` SDK involved (the handlers carry no such
dependency; that is the next task's concern). Coverage:

* :func:`resolve_ledger_root` — explicit ``root`` wins, else
  ``$CLAUDE_LEDGER_ROOT``, else the per-user default (XDG data dir); blank env
  falls through.
* :func:`ledger_log` — appends a CloudEvent (``id`` / ``time`` / ``seq``
  filled), coerces JSON-string and object ``data``, honors an explicit machine,
  and persists to the resolved root.
* :func:`ledger_query` — filters by type-glob / source / machine / time bounds,
  orders by ``(time, seq)``, applies ``limit``, tolerates a missing root.
* :func:`ledger_stats` — counts by type/source, event rate, optional paired
  durations; rejects bad ``by`` / ``rate_unit``.

The handlers import only the public ``evledger`` API, so these run as
plain unit tests with no server runtime.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evledger import LedgerStore, new_event
from evledger.mcp import (
    ledger_log,
    ledger_query,
    ledger_stats,
    resolve_ledger_root,
)

# --- root resolution (pure) -----------------------------------------------


def test_resolve_root_explicit_arg_wins(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit"
    out = resolve_ledger_root(
        root=explicit,
        env={"CLAUDE_LEDGER_ROOT": str(tmp_path / "envroot")},
        cwd=tmp_path,
    )
    assert out == explicit


def test_resolve_root_env_when_no_arg(tmp_path: Path) -> None:
    envroot = tmp_path / "envroot"
    out = resolve_ledger_root(
        root=None,
        env={"CLAUDE_LEDGER_ROOT": str(envroot)},
        cwd=tmp_path,
    )
    assert out == envroot


def test_resolve_root_blank_env_falls_through_to_default(tmp_path: Path) -> None:
    out = resolve_ledger_root(
        root=None,
        env={"CLAUDE_LEDGER_ROOT": "   ", "XDG_DATA_HOME": str(tmp_path)},
        cwd=tmp_path,
    )
    assert out == tmp_path / "evledger" / "ledger"


def test_resolve_root_default_is_user_data_dir(tmp_path: Path) -> None:
    # No root, no env: per-user data dir, NOT cwd-relative. cwd is ignored.
    out = resolve_ledger_root(
        root=None, env={"XDG_DATA_HOME": str(tmp_path)}, cwd=tmp_path / "elsewhere"
    )
    assert out == tmp_path / "evledger" / "ledger"


# --- ledger_log -----------------------------------------------------------


def test_log_appends_event_with_filled_fields(tmp_path: Path) -> None:
    result = ledger_log(
        source="/m1/dev",
        type="dev.example.thing",
        machine="m1",
        root=tmp_path,
    )
    # id / time / seq are filled in the returned CloudEvents dict.
    assert result["source"] == "/m1/dev"
    assert result["type"] == "dev.example.thing"
    assert result["machine"] == "m1"
    assert result["specversion"] == "1.0"
    assert isinstance(result["id"], str) and result["id"]
    assert result["time"].endswith("Z")
    assert result["seq"] == 0  # first event in a fresh machine partition

    # And it is actually persisted under the resolved root.
    stored = LedgerStore(root=tmp_path).read_all()
    assert len(stored.events) == 1
    assert stored.events[0].id == result["id"]


def test_log_assigns_monotonic_seq(tmp_path: Path) -> None:
    first = ledger_log(source="/m1/dev", type="dev.x.a", machine="m1", root=tmp_path)
    second = ledger_log(source="/m1/dev", type="dev.x.b", machine="m1", root=tmp_path)
    assert first["seq"] == 0
    assert second["seq"] == 1


def test_log_coerces_json_string_data(tmp_path: Path) -> None:
    result = ledger_log(
        source="/m1/dev",
        type="dev.x.thing",
        data='{"detail": "hi", "n": 3}',
        machine="m1",
        root=tmp_path,
    )
    assert result["data"] == {"detail": "hi", "n": 3}


def test_log_accepts_object_data(tmp_path: Path) -> None:
    result = ledger_log(
        source="/m1/dev",
        type="dev.x.thing",
        data={"detail": "hi"},
        machine="m1",
        root=tmp_path,
    )
    assert result["data"] == {"detail": "hi"}


def test_log_non_json_string_data_kept_verbatim(tmp_path: Path) -> None:
    result = ledger_log(
        source="/m1/dev",
        type="dev.x.thing",
        data="just a string",
        machine="m1",
        root=tmp_path,
    )
    assert result["data"] == "just a string"


def test_log_omits_data_when_none(tmp_path: Path) -> None:
    result = ledger_log(source="/m1/dev", type="dev.x.thing", machine="m1", root=tmp_path)
    assert "data" not in result


def test_log_uses_env_ledger_root(tmp_path: Path) -> None:
    envroot = tmp_path / "envroot"
    ledger_log(
        source="/m1/dev",
        type="dev.x.thing",
        machine="m1",
        env={"CLAUDE_LEDGER_ROOT": str(envroot)},
    )
    assert LedgerStore(root=envroot).read_all().events  # written under env root


def test_log_resolves_machine_from_env(tmp_path: Path) -> None:
    result = ledger_log(
        source="/auto/dev",
        type="dev.x.thing",
        root=tmp_path,
        env={"CLAUDE_MACHINE_ID": "envmachine"},
    )
    assert result["machine"] == "envmachine"


# --- ledger_query ---------------------------------------------------------


def _seed(root: Path) -> None:
    """Seed a small fixed ledger: three events on m1, one on m2."""
    store = LedgerStore(root=root)
    store.append(
        new_event(source="/m1/dev", type="dev.x.mission.start", machine="m1",
                  time="2026-06-01T00:00:00Z")
    )
    store.append(
        new_event(source="/m1/dev", type="dev.x.mission.end", machine="m1",
                  time="2026-06-01T01:00:00Z")
    )
    store.append(
        new_event(source="/m1/build", type="dev.x.build", machine="m1",
                  time="2026-06-02T00:00:00Z")
    )
    store.append(
        new_event(source="/m2/dev", type="dev.x.mission.start", machine="m2",
                  time="2026-06-03T00:00:00Z")
    )


def test_query_returns_all_ordered(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = ledger_query(root=tmp_path)
    assert result["count"] == 4
    times = [e["time"] for e in result["events"]]
    assert times == sorted(times)  # ordered by (time, seq)


def test_query_type_glob_filter(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = ledger_query(type="dev.x.mission.*", root=tmp_path)
    assert result["count"] == 3
    assert all(e["type"].startswith("dev.x.mission.") for e in result["events"])


def test_query_machine_filter(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = ledger_query(machine="m2", root=tmp_path)
    assert result["count"] == 1
    assert result["events"][0]["machine"] == "m2"


def test_query_source_filter(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = ledger_query(source="/m1/build", root=tmp_path)
    assert result["count"] == 1
    assert result["events"][0]["source"] == "/m1/build"


def test_query_time_bounds(tmp_path: Path) -> None:
    _seed(tmp_path)
    # since inclusive, until exclusive: [06-01T01, 06-03) -> end + build only.
    result = ledger_query(
        since="2026-06-01T01:00:00Z",
        until="2026-06-03T00:00:00Z",
        root=tmp_path,
    )
    types = {e["type"] for e in result["events"]}
    assert types == {"dev.x.mission.end", "dev.x.build"}


def test_query_limit_keeps_most_recent(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = ledger_query(limit=2, root=tmp_path)
    assert result["count"] == 2
    # Last two in (time, seq) order are the build (06-02) and m2 start (06-03).
    assert [e["time"] for e in result["events"]] == [
        "2026-06-02T00:00:00Z",
        "2026-06-03T00:00:00Z",
    ]


def test_query_limit_zero_returns_none(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = ledger_query(limit=0, root=tmp_path)
    assert result["count"] == 0
    assert result["events"] == []


def test_query_missing_root_is_empty(tmp_path: Path) -> None:
    result = ledger_query(root=tmp_path / "does-not-exist")
    assert result == {"count": 0, "events": []}


# --- ledger_stats ---------------------------------------------------------


def test_stats_counts_by_type(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = ledger_stats(root=tmp_path)
    assert result["total"] == 4
    assert result["by"] == "type"
    assert result["counts"] == {
        "dev.x.mission.start": 2,
        "dev.x.mission.end": 1,
        "dev.x.build": 1,
    }


def test_stats_counts_by_source(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = ledger_stats(by="source", root=tmp_path)
    assert result["by"] == "source"
    assert result["counts"] == {"/m1/dev": 2, "/m1/build": 1, "/m2/dev": 1}


def test_stats_rate_shape(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = ledger_stats(rate_unit="hour", root=tmp_path)
    rate = result["rate"]
    assert rate["count"] == 4
    assert rate["unit"] == "hour"
    assert rate["start"] is not None
    assert rate["end"] is not None
    assert isinstance(rate["per_unit"], float)


def test_stats_no_durations_key_without_pair(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = ledger_stats(root=tmp_path)
    assert "durations" not in result


def test_stats_pair_durations(tmp_path: Path) -> None:
    _seed(tmp_path)
    # m1 mission.start@00:00 -> mission.end@01:00 = 3600s; m2 start is unmatched.
    result = ledger_stats(type="dev.x.mission.*", pair=True, root=tmp_path)
    durations = result["durations"]
    assert durations["matched"] == 1
    assert durations["unmatched_starts"] == 1
    assert durations["unmatched_ends"] == 0
    assert durations["total_seconds"] == 3600.0
    assert durations["max_seconds"] == 3600.0


def test_stats_rejects_bad_by(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ledger_stats(by="machine", root=tmp_path)


def test_stats_rejects_bad_rate_unit(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ledger_stats(rate_unit="fortnight", root=tmp_path)


def test_stats_empty_ledger(tmp_path: Path) -> None:
    result = ledger_stats(root=tmp_path / "empty")
    assert result["total"] == 0
    assert result["counts"] == {}
    assert result["rate"]["count"] == 0


# --- SDK independence -----------------------------------------------------


def test_tools_module_does_not_import_mcp() -> None:
    import evledger.mcp.tools as tools_mod

    # The tools module must not pull in the MCP SDK at import time. Assert on
    # the source (robust) rather than sys.modules — a sibling test that imports
    # the SDK pollutes sys.modules within the shared pytest process.
    source = Path(tools_mod.__file__).read_text(encoding="utf-8")
    assert "import mcp" not in source
    assert "from mcp" not in source
    assert "fastmcp" not in source.lower()
