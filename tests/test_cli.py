"""CLI integration tests for the ``claude-kg ledger`` subcommand group.

Exercises the group via Click's :class:`CliRunner`:

* ``ledger log`` appends a CloudEvent to the right machine partition with an
  assigned ``seq``; ``--data`` parses JSON; ``--format json`` round-trips.
* ``ledger show`` lists matching events ordered by ``(time, seq)``; filters
  (``--type`` glob, ``--source``, ``--machine``, ``--since``/``--until``) and
  ``--limit`` apply; missing root is tolerated.
* ``ledger stats`` reports counts (by type / source), event rate, and (with
  ``--pair``) paired ``*.start``/``*.end`` durations.
* Root resolution: ``--ledger-root`` flag wins, else ``$CLAUDE_LEDGER_ROOT``,
  else the per-user default (XDG data dir).

These cover the public CLI surface; the ledger core layers have their own
unit tests, so this focuses on wiring + output shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from evledger import LedgerStore, new_event
from evledger.cli import ledger_group, resolve_ledger_root
from evledger.reconstruct import ModelUnavailableError


@click.group()
def cli() -> None:
    """Test harness root that mounts the ledger group under ``ledger``.

    Standalone, evledger's console script is ``ledger_group`` directly
    (``evledger log ...``); these tests exercise it through a parent group so
    the ``["ledger", ...]`` invocation paths read the same as in production.
    """


cli.add_command(ledger_group)


# --- root resolution (pure) -----------------------------------------------


def test_resolve_ledger_root_flag_wins(tmp_path: Path) -> None:
    flag = tmp_path / "explicit"
    out = resolve_ledger_root(
        ledger_root=flag,
        repo_root=tmp_path,
        env={"CLAUDE_LEDGER_ROOT": str(tmp_path / "envroot")},
    )
    assert out == flag


def test_resolve_ledger_root_env_when_no_flag(tmp_path: Path) -> None:
    envroot = tmp_path / "envroot"
    out = resolve_ledger_root(
        ledger_root=None,
        repo_root=tmp_path,
        env={"CLAUDE_LEDGER_ROOT": str(envroot)},
    )
    assert out == envroot


def test_resolve_ledger_root_blank_env_falls_through(tmp_path: Path) -> None:
    out = resolve_ledger_root(
        ledger_root=None,
        repo_root=tmp_path,
        env={"CLAUDE_LEDGER_ROOT": "   ", "XDG_DATA_HOME": str(tmp_path)},
    )
    assert out == tmp_path / "evledger" / "ledger"


def test_resolve_ledger_root_default_is_user_data_dir(tmp_path: Path) -> None:
    # No flag, no env: the default is the per-user data dir, NOT repo-relative.
    # repo_root is passed but must be ignored (retained only for compatibility).
    out = resolve_ledger_root(
        ledger_root=None,
        repo_root=tmp_path / "repo",
        env={"XDG_DATA_HOME": str(tmp_path)},
    )
    assert out == tmp_path / "evledger" / "ledger"


# --- ledger log -----------------------------------------------------------


def test_log_appends_event_to_machine_partition(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "ledger", "log",
            "--source", "/laptop/dev",
            "--type", "dev.example.mission.start",
            "--machine", "laptop",
            "--ledger-root", str(root),
        ],
    )

    assert result.exit_code == 0, result.output
    # One event landed in the laptop partition; seq assigned at 0.
    events = LedgerStore(root=root).read_all().events
    assert len(events) == 1
    e = events[0]
    assert e.source == "/laptop/dev"
    assert e.type == "dev.example.mission.start"
    assert e.machine == "laptop"
    assert e.seq == 0


def test_log_parses_data_json(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "ledger", "log",
            "--source", "/laptop/dev",
            "--type", "dev.example.thing",
            "--machine", "laptop",
            "--data", '{"detail": "hi", "n": 3}',
            "--ledger-root", str(root),
        ],
    )

    assert result.exit_code == 0, result.output
    e = LedgerStore(root=root).read_all().events[0]
    assert e.data == {"detail": "hi", "n": 3}


def test_log_rejects_invalid_data_json(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "ledger", "log",
            "--source", "/laptop/dev",
            "--type", "dev.example.thing",
            "--machine", "laptop",
            "--data", "{not json}",
            "--ledger-root", str(root),
        ],
    )

    assert result.exit_code == 2  # click.BadParameter -> usage error
    # Nothing written.
    assert LedgerStore(root=root).read_all().events == []


def test_log_requires_source_and_type(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["ledger", "log", "--machine", "laptop", "--ledger-root", str(root)],
    )
    assert result.exit_code == 2


def test_log_json_output_is_the_stored_event(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "ledger", "log",
            "--source", "/laptop/dev",
            "--type", "dev.example.thing",
            "--machine", "laptop",
            "--ledger-root", str(root),
            "--format", "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["seq"] == 0
    assert payload["type"] == "dev.example.thing"
    assert payload["specversion"] == "1.0"


def test_log_uses_env_ledger_root(tmp_path: Path) -> None:
    root = tmp_path / "envledger"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "ledger", "log",
            "--source", "/laptop/dev",
            "--type", "dev.example.thing",
            "--machine", "laptop",
        ],
        env={"CLAUDE_LEDGER_ROOT": str(root)},
    )
    assert result.exit_code == 0, result.output
    assert LedgerStore(root=root).read_all().events[0].machine == "laptop"


# --- ledger show ----------------------------------------------------------


def _seed(root: Path) -> LedgerStore:
    """Seed a store with a small mixed set of events."""
    store = LedgerStore(root=root)
    store.append(new_event(source="/laptop/dev", type="dev.x.mission.start",
                           machine="laptop", time="2026-05-29T10:00:00Z"))
    store.append(new_event(source="/laptop/dev", type="dev.x.mission.end",
                           machine="laptop", time="2026-05-29T10:30:00Z"))
    store.append(new_event(source="/server/ci", type="dev.x.build.start",
                           machine="server", time="2026-05-29T11:00:00Z"))
    return store


def test_show_missing_root_is_tolerated(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["ledger", "show", "--ledger-root", str(tmp_path / "nope")]
    )
    assert result.exit_code == 0, result.output
    assert "no matching events" in result.output


def test_show_lists_all_in_time_order(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    _seed(root)
    runner = CliRunner()
    result = runner.invoke(cli, ["ledger", "show", "--ledger-root", str(root)])
    assert result.exit_code == 0, result.output
    # Earliest first; the start event's time precedes the others in the output.
    idx_start = result.output.index("dev.x.mission.start")
    idx_end = result.output.index("dev.x.mission.end")
    idx_build = result.output.index("dev.x.build.start")
    assert idx_start < idx_end < idx_build
    assert "3 event(s)." in result.output


def test_show_type_glob_filter(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    _seed(root)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["ledger", "show", "--ledger-root", str(root),
         "--type", "dev.x.mission.*", "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    events = json.loads(result.output)
    assert {e["type"] for e in events} == {
        "dev.x.mission.start", "dev.x.mission.end"
    }


def test_show_machine_filter(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    _seed(root)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["ledger", "show", "--ledger-root", str(root),
         "--machine", "server", "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    events = json.loads(result.output)
    assert len(events) == 1
    assert events[0]["machine"] == "server"


def test_show_time_range_filter(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    _seed(root)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["ledger", "show", "--ledger-root", str(root),
         "--since", "2026-05-29T10:15:00Z", "--until", "2026-05-29T11:00:00Z",
         "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    events = json.loads(result.output)
    # since is inclusive, until is exclusive: only the 10:30 end event.
    assert len(events) == 1
    assert events[0]["type"] == "dev.x.mission.end"


def test_show_limit_keeps_most_recent(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    _seed(root)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["ledger", "show", "--ledger-root", str(root),
         "--limit", "1", "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    events = json.loads(result.output)
    assert len(events) == 1
    # Most recent of the three (server build at 11:00).
    assert events[0]["type"] == "dev.x.build.start"


# --- ledger stats ---------------------------------------------------------


def test_stats_counts_by_type_json(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    _seed(root)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["ledger", "stats", "--ledger-root", str(root), "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total"] == 3
    assert payload["by"] == "type"
    assert payload["counts"] == {
        "dev.x.mission.start": 1,
        "dev.x.mission.end": 1,
        "dev.x.build.start": 1,
    }


def test_stats_counts_by_source(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    _seed(root)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["ledger", "stats", "--ledger-root", str(root),
         "--by", "source", "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["by"] == "source"
    assert payload["counts"] == {"/laptop/dev": 2, "/server/ci": 1}


def test_stats_rate_per_hour(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    _seed(root)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["ledger", "stats", "--ledger-root", str(root),
         "--rate-unit", "hour", "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    rate = json.loads(result.output)["rate"]
    # Observed span: 10:00 -> 11:00 = 3600s, 3 events => 3 events/hour.
    assert rate["count"] == 3
    assert rate["span_seconds"] == 3600.0
    assert rate["unit"] == "hour"
    assert abs(rate["per_unit"] - 3.0) < 1e-9


def test_stats_pair_durations(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    _seed(root)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["ledger", "stats", "--ledger-root", str(root),
         "--type", "dev.x.mission.*", "--pair", "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    d = payload["durations"]
    # mission.start (10:00) -> mission.end (10:30) = 1800s, one matched pair.
    assert d["matched"] == 1
    assert d["total_seconds"] == 1800.0
    assert d["min_seconds"] == 1800.0
    assert d["max_seconds"] == 1800.0
    assert d["unmatched_starts"] == 0
    assert d["unmatched_ends"] == 0


def test_stats_text_output_smoke(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    _seed(root)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["ledger", "stats", "--ledger-root", str(root)]
    )
    assert result.exit_code == 0, result.output
    assert "Events: 3" in result.output
    assert "Counts by type" in result.output
    assert "events/hour" in result.output


# --- ledger digest --------------------------------------------------------


def _seed_oversight(root: Path) -> LedgerStore:
    """Seed a store with oversight events that trip several digest rules."""
    store = LedgerStore(root=root)
    store.append(new_event(
        source="claude-config", type="dev.claude.decision.autonomous",
        machine="laptop", time="2026-05-29T09:00:00Z",
        data={"summary": "picked lib A", "could_have_asked": True}))
    store.append(new_event(
        source="claude-config", type="dev.claude.failure",
        machine="laptop", time="2026-05-29T10:00:00Z",
        data={"kind": "test", "summary": "boom"}))
    store.append(new_event(
        source="claude-config", type="dev.claude.push",
        machine="laptop", time="2026-05-29T10:30:00Z",
        data={"branch": "main"}))
    store.append(new_event(
        source="claude-config", type="dev.claude.task.shipped",
        machine="laptop", time="2026-05-29T11:00:00Z",
        data={"whim": "w", "task": "t", "pr": "#1", "tokens": 100}))
    store.append(new_event(
        source="claude-config", type="dev.claude.task.shipped",
        machine="laptop", time="2026-05-29T11:15:00Z",
        data={"whim": "w", "task": "t2", "pr": "#2", "tokens": 100}))
    store.append(new_event(
        source="claude-config", type="dev.claude.task.shipped",
        machine="laptop", time="2026-05-29T11:30:00Z",
        data={"whim": "w", "task": "t3", "pr": "#3", "tokens": 1000}))
    store.append(new_event(
        source="claude-config", type="dev.claude.refusal",
        machine="laptop", time="2026-05-29T12:00:00Z",
        data={"action": "force-push", "reason": "rewrites history"}))
    return store


def test_digest_missing_root_is_tolerated(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["ledger", "digest", "--ledger-root", str(tmp_path / "nope")]
    )
    assert result.exit_code == 0, result.output
    assert "Events: 0" in result.output


def test_digest_json_reports_rollup_and_anomalies(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    _seed_oversight(root)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["ledger", "digest", "--ledger-root", str(root), "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    assert payload["total"] == 7
    assert payload["resources"]["shipped"] == 3
    assert payload["resources"]["tokens"] == 1200.0

    rules = {a["rule"] for a in payload["anomalies"]}
    assert "could_have_asked" in rules
    assert "failure" in rules
    assert "push_without_green" in rules
    assert "token_outlier" in rules
    assert "refusal" in rules


def test_digest_text_output_smoke(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    _seed_oversight(root)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["ledger", "digest", "--ledger-root", str(root)]
    )
    assert result.exit_code == 0, result.output
    assert "Events: 7" in result.output
    assert "Anomalies:" in result.output
    assert "could_have_asked" in result.output


def test_digest_spike_threshold_flag(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    store = LedgerStore(root=root)
    for i in range(3):
        store.append(new_event(
            source="claude-config", type="dev.claude.failure",
            machine="laptop", time=f"2026-05-29T1{i}:00:00Z",
            data={"kind": "test", "summary": "x"}))
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["ledger", "digest", "--ledger-root", str(root),
         "--spike-threshold", "3", "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    rules = {a["rule"] for a in json.loads(result.output)["anomalies"]}
    assert "failure_spike" in rules


# --- ledger audit ---------------------------------------------------------
#
# The model call sits behind ``_make_model_client``; every test here either
# monkeypatches that seam to return a canned fake (no network) or to raise
# ModelUnavailableError (the degrade path). No live model is ever touched.


class _FakeAuditClient:
    """A canned model client: returns the same scripted JSON for every chunk."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.calls = 0

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls += 1
        return self._response


_AUDIT_RESPONSE = json.dumps(
    {
        "events": [
            {
                "kind": "decision.autonomous",
                "data": {
                    "summary": "chose the Agent SDK over a raw API key",
                    "could_have_asked": True,
                },
                "time": "2026-05-30T10:05:00Z",
            }
        ],
        "necessity": [
            {
                "target_kind": "permission.requested",
                "label": "rote",
                "summary": "asked before editing the only matching file",
                "rationale": "no real alternative existed",
            }
        ],
    }
)


def _seed_transcript(projects_root: Path, session_id: str = "sess-1") -> None:
    """Write a minimal Claude Code session JSONL transcript under a project dir."""
    project_dir = projects_root / "-some-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({
            "type": "user", "sessionId": session_id, "uuid": "u1",
            "timestamp": "2026-05-30T10:00:00Z",
            "message": {"role": "user", "content": "refactor the auth module"},
        }),
        json.dumps({
            "type": "assistant", "sessionId": session_id, "uuid": "a1",
            "timestamp": "2026-05-30T10:05:00Z",
            "message": {"role": "assistant", "content": "I split it into two files"},
        }),
    ]
    (project_dir / f"{session_id}.jsonl").write_text("\n".join(lines), encoding="utf-8")


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: object) -> None:
    monkeypatch.setattr(
        "evledger.cli._make_model_client", lambda model: client
    )


def test_audit_empty_window_is_a_noop(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["ledger", "audit",
         "--projects-root", str(tmp_path / "empty"),
         "--ledger-root", str(tmp_path / "ledger")],
    )
    assert result.exit_code == 0, result.output
    assert "nothing to audit" in result.output


def test_audit_degrades_without_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects"
    _seed_transcript(projects)

    def _raise(model: str | None) -> object:
        raise ModelUnavailableError("claude-agent-sdk is not installed")

    monkeypatch.setattr("evledger.cli._make_model_client", _raise)

    root = tmp_path / "ledger"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["ledger", "audit",
         "--projects-root", str(projects),
         "--ledger-root", str(root)],
    )
    # Clear no-op, NOT a crash.
    assert result.exit_code == 0, result.output
    assert "No model credential available" in result.output
    # Nothing was appended.
    assert LedgerStore(root=root).read_all().events == []


def test_audit_dry_run_emits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects"
    _seed_transcript(projects)
    _patch_client(monkeypatch, _FakeAuditClient(_AUDIT_RESPONSE))

    root = tmp_path / "ledger"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["ledger", "audit",
         "--projects-root", str(projects),
         "--ledger-root", str(root),
         "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # Dry-run is the default: a candidate was found but NOTHING was appended.
    assert payload["dry_run"] is True
    assert payload["reconstructed"]["novel"] == 1
    assert payload["reconstructed"]["appended"] == []
    # The ledger on disk is untouched.
    assert LedgerStore(root=root).read_all().events == []


def test_audit_no_dry_run_appends_reconstructed_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects"
    _seed_transcript(projects)
    _patch_client(monkeypatch, _FakeAuditClient(_AUDIT_RESPONSE))

    root = tmp_path / "ledger"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["ledger", "audit", "--no-dry-run",
         "--projects-root", str(projects),
         "--ledger-root", str(root),
         "--machine", "laptop",
         "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is False
    assert len(payload["reconstructed"]["appended"]) == 1

    # The reconstructed event landed with its provenance markers.
    events = LedgerStore(root=root).read_all().events
    assert len(events) == 1
    e = events[0]
    assert e.source == "oversight-analyzer"
    assert e.type == "dev.claude.decision.autonomous"
    assert e.machine == "laptop"
    assert e.data["reconstructed"] is True
    assert e.data["session_id"] == "sess-1"
    assert e.data["summary"] == "chose the Agent SDK over a raw API key"


def test_audit_is_idempotent_across_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects"
    _seed_transcript(projects)
    _patch_client(monkeypatch, _FakeAuditClient(_AUDIT_RESPONSE))

    root = tmp_path / "ledger"
    runner = CliRunner()
    args = [
        "ledger", "audit", "--no-dry-run",
        "--projects-root", str(projects),
        "--ledger-root", str(root),
        "--machine", "laptop",
    ]
    first = runner.invoke(cli, args)
    assert first.exit_code == 0, first.output
    second = runner.invoke(cli, args)
    assert second.exit_code == 0, second.output

    # Re-auditing the same session did NOT double-emit (dedup by
    # (session_id, signature)).
    events = LedgerStore(root=root).read_all().events
    assert len(events) == 1


def test_audit_text_output_reports_necessity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects"
    _seed_transcript(projects)
    _patch_client(monkeypatch, _FakeAuditClient(_AUDIT_RESPONSE))

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["ledger", "audit",
         "--projects-root", str(projects),
         "--ledger-root", str(tmp_path / "ledger")],
    )
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    assert "Necessity report:" in result.output
    assert "[rote] permission.requested" in result.output
    assert "decision.autonomous" in result.output


# --- ledger serve (host wiring) -------------------------------------------


def test_serve_forwards_host_and_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`serve --host 0.0.0.0 --port N` threads both through to viz.server.serve."""
    import evledger.viz.server as viz

    captured: dict[str, object] = {}

    def fake_serve(*, root: Path, port: int, host: str, open_browser: bool) -> None:
        captured.update(root=root, port=port, host=host, open_browser=open_browser)

    monkeypatch.setattr(viz, "serve", fake_serve)
    result = CliRunner().invoke(
        cli,
        ["ledger", "serve", "--host", "0.0.0.0", "--port", "9999", "--no-open",
         "--ledger-root", str(tmp_path / "ledger")],
    )
    assert result.exit_code == 0, result.output
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9999
    assert captured["open_browser"] is False


def test_serve_defaults_to_localhost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no --host, serve binds localhost (safe default)."""
    import evledger.viz.server as viz

    captured: dict[str, object] = {}
    monkeypatch.setattr(viz, "serve", lambda **kw: captured.update(kw))
    result = CliRunner().invoke(
        cli, ["ledger", "serve", "--no-open", "--ledger-root", str(tmp_path / "ledger")]
    )
    assert result.exit_code == 0, result.output
    assert captured["host"] == "127.0.0.1"
