"""``claude-kg ledger`` CLI surface (the ``ledger-cli`` task).

This is the **consumer layer** that wires the standalone ledger core
(:mod:`evledger`) to the repo's ``click``-based CLI, matching the
style of the other ``claude-kg`` subcommands. Per Decision 9 the ledger
*core* stays stdlib-only and standalone; this layer is allowed to use
``click`` because it is the consumer, but it imports ledger functionality
**only** from the public :mod:`evledger` package surface — never from
its internal submodules — so the core stays extractable.

Three subcommands:

* ``ledger log --source S --type T [--data JSON]`` — append one CloudEvent to
  the current machine's partition.
* ``ledger show [--since/--until/--source/--type/--machine]`` — query and list
  matching events, ordered by ``(time, seq)``.
* ``ledger stats [--source/--type/--machine/--since/--until] [--by ...]
  [--rate-unit ...] [--pair]`` — descriptive aggregations (counts, rate,
  paired ``*.start``/``*.end`` durations) over the matching events.
* ``ledger digest [--since/--until/--source] [--format text|json]`` — a
  **deterministic** oversight report: the rollup (counts / tokens / cost /
  durations / recent notables) plus rule-based anomaly flags (could-have-asked
  decisions, failures + spikes, push-without-green, token/duration outliers,
  refusals). No model; pure computation over the event stream.
* ``ledger audit [--since/--until/--session] [--dry-run/--no-dry-run]
  [--format text|json]`` — the **model-powered** oversight finale: an
  independent LLM pass that reads the session transcripts for a window and
  reconstructs the ``decision.autonomous`` / ``failure`` / ``refusal`` events
  the live self-emit layer missed (stamped with ``source=oversight-analyzer`` +
  ``data.reconstructed`` provenance), plus a needed-vs-rote necessity report.
  ``--dry-run`` is the **default** (safe): it previews what it *would* append
  without writing anything; ``--no-dry-run`` appends the reconstructed events
  best-effort. Append-only — it never mutates or deletes existing events, and
  it dedups against its own prior output by ``(session_id, signature)`` so
  re-auditing never double-emits. When no model credential / SDK is available
  it degrades to a clear no-op (no crash). The model call sits behind an
  injectable client (the real one rides the Claude Agent SDK / local Claude
  Code auth); the deterministic plumbing is tested with a fake client.

Every subcommand resolves the ledger *instance* root (Decision 8) via
:func:`resolve_ledger_root`: the ``--ledger-root`` flag, else
``$CLAUDE_LEDGER_ROOT``, else ``<repo-root>/ledger`` (``--repo-root`` defaults
to the current directory). No hardcoded ``~/.claude`` path.

All three support ``--format {text,json}`` (default ``text``), matching the
existing CLI output convention.
"""

from __future__ import annotations

import json as _json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import click

from evledger import (
    CLAUDE_DIGEST_CONFIG,
    AgentSdkModelClient,
    Anomaly,
    AuditResult,
    CandidateEvent,
    Digest,
    DurationStats,
    LedgerStore,
    ModelClient,
    ModelUnavailableError,
    NecessityLabel,
    Query,
    ReconstructConfig,
    TranscriptChunk,
    chunk_turns,
    counts_by_source,
    counts_by_type,
    dedup_candidates,
    digest as compute_digest,
    event_rate,
    existing_signatures,
    load_transcripts,
    new_event,
    pair_events,
    query_events,
    reconstruct_chunks,
    resolve_machine_id,
    to_ledger_event,
)

#: Environment variable consulted for the ledger instance root.
LEDGER_ROOT_ENV_VAR = "CLAUDE_LEDGER_ROOT"

#: Recognized ``--rate-unit`` values mapped to their :class:`~datetime.timedelta`.
_RATE_UNITS: dict[str, timedelta] = {
    "second": timedelta(seconds=1),
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
}


def resolve_ledger_root(
    ledger_root: Path | None,
    repo_root: Path,
    env: dict[str, str] | None = None,
) -> Path:
    """Resolve the ledger *instance* root directory (Decision 8).

    Resolution order:

    1. The explicit ``--ledger-root`` flag, if given.
    2. The ``$CLAUDE_LEDGER_ROOT`` environment variable, if set and non-blank.
    3. ``<repo-root>/ledger`` as the generic default.

    No hardcoded ``~/.claude`` path: the only home-relative default lives in
    the machine-id resolver, not here.

    Args:
        ledger_root: The value of the ``--ledger-root`` flag (``None`` if unset).
        repo_root: The repo root used to derive the default ``ledger/`` path.
        env: Environment mapping to read ``$CLAUDE_LEDGER_ROOT`` from. Defaults
            to ``os.environ``.

    Returns:
        The resolved (unresolved-symlink) ledger root path.
    """
    if ledger_root is not None:
        return ledger_root
    environ = env if env is not None else os.environ
    env_value = environ.get(LEDGER_ROOT_ENV_VAR)
    if env_value is not None and env_value.strip():
        return Path(env_value)
    return repo_root / "ledger"


def _parse_data(raw: str | None) -> Any | None:
    """Parse the ``--data`` JSON argument, or ``None`` when not supplied.

    Raises:
        click.BadParameter: if ``raw`` is not valid JSON.
    """
    if raw is None:
        return None
    try:
        return _json.loads(raw)
    except _json.JSONDecodeError as exc:
        raise click.BadParameter(f"--data must be valid JSON: {exc}") from exc


def _event_to_dict(event: Any) -> dict[str, Any]:
    """Render an event as its CloudEvents dict (for JSON output)."""
    return event.to_dict()


def _format_event_line(event: Any) -> str:
    """Render one event as a compact human-readable line for text output."""
    seq = event.seq if event.seq is not None else "-"
    base = f"{event.time}  seq={seq}  {event.machine}  {event.type}  {event.source}"
    if event.data is not None:
        base += f"  data={_json.dumps(event.data, ensure_ascii=False, separators=(',', ':'))}"
    return base


# -- the group -------------------------------------------------------------


@click.group(name="ledger")
def ledger_group() -> None:
    """Append to and inspect the local CloudEvents ledger.

    The ledger is a git-backed, append-only, machine-partitioned JSONL event
    store. ``log`` appends events; ``show`` queries them; ``stats`` computes
    descriptive aggregations; ``digest`` is a deterministic oversight report;
    ``audit`` is the model-powered oversight pass that reconstructs the
    oversight events the live self-emit layer missed. The instance root resolves
    via ``--ledger-root`` → ``$CLAUDE_LEDGER_ROOT`` → ``<repo-root>/ledger``.

    To expose these same operations to MCP clients (Claude Desktop, other
    agents) over stdio, install the optional MCP extra and run the dedicated
    launcher: ``pip install claude-kg[mcp]`` then ``claude-kg-ledger-mcp``
    (kept as a separate console script so this CLI never imports the MCP SDK).
    """


def _ledger_root_option(fn):  # type: ignore[no-untyped-def]
    """Attach the shared ``--ledger-root`` / ``--repo-root`` options."""
    fn = click.option(
        "--ledger-root",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        help="Ledger instance root. Default: $CLAUDE_LEDGER_ROOT, else <repo-root>/ledger.",
    )(fn)
    fn = click.option(
        "--repo-root",
        type=click.Path(exists=True, file_okay=False, path_type=Path),
        default=Path("."),
        help="Repo root (default: cwd). Used to derive the default <repo-root>/ledger.",
    )(fn)
    return fn


@ledger_group.command(name="log")
@click.option("--source", required=True, help="CloudEvents source URI-reference (e.g. /<machine>/<system>).")
@click.option("--type", "event_type", required=True, help="Reverse-DNS event type (e.g. dev.example.mission.start).")
@click.option("--data", "data_raw", default=None, help="Optional JSON payload for the event's data attribute.")
@click.option("--machine", default=None, help="Machine partition key. Default: resolved machine id.")
@_ledger_root_option
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text", show_default=True)
def log_cmd(
    source: str,
    event_type: str,
    data_raw: str | None,
    machine: str | None,
    repo_root: Path,
    ledger_root: Path | None,
    fmt: str,
) -> None:
    """Append one CloudEvent to the ledger.

    The event's ``id`` and ``time`` are generated; the store assigns the
    monotonic per-machine ``seq``. Prints the stored event.
    """
    root = resolve_ledger_root(ledger_root, repo_root.resolve())
    machine_id = machine if machine is not None else resolve_machine_id()
    data = _parse_data(data_raw)

    event = new_event(source=source, type=event_type, machine=machine_id, data=data)
    store = LedgerStore(root=root)
    stored = store.append(event)

    if fmt == "json":
        click.echo(_json.dumps(_event_to_dict(stored), indent=2))
    else:
        click.echo(f"logged → {root / stored.machine}")
        click.echo(_format_event_line(stored))


@ledger_group.command(name="show")
@click.option("--source", default=None, help="Filter: exact CloudEvents source.")
@click.option("--type", "event_type", default=None, help="Filter: event type (shell glob, e.g. dev.x.mission.*).")
@click.option("--machine", default=None, help="Filter: exact machine partition key.")
@click.option("--since", default=None, help="Inclusive lower time bound (ISO-8601, Z or offset).")
@click.option("--until", default=None, help="Exclusive upper time bound (ISO-8601, Z or offset).")
@click.option("--limit", type=int, default=None, help="Cap the number of events shown (most-recent-last order preserved).")
@_ledger_root_option
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text", show_default=True)
def show_cmd(
    source: str | None,
    event_type: str | None,
    machine: str | None,
    since: str | None,
    until: str | None,
    limit: int | None,
    repo_root: Path,
    ledger_root: Path | None,
    fmt: str,
) -> None:
    """List ledger events matching the filters, ordered by (time, seq)."""
    root = resolve_ledger_root(ledger_root, repo_root.resolve())
    store = LedgerStore(root=root)
    query = Query(
        source=source,
        type=event_type,
        machine=machine,
        since=since,
        until=until,
    )
    events = query_events(store.iter_events(), query)
    if limit is not None and limit >= 0:
        events = events[-limit:] if limit else []

    if fmt == "json":
        click.echo(_json.dumps([_event_to_dict(e) for e in events], indent=2))
    else:
        if not events:
            click.echo("(no matching events)")
            return
        for e in events:
            click.echo(_format_event_line(e))
        click.echo(f"\n{len(events)} event(s).")


@ledger_group.command(name="stats")
@click.option("--source", default=None, help="Filter: exact CloudEvents source.")
@click.option("--type", "event_type", default=None, help="Filter: event type (shell glob).")
@click.option("--machine", default=None, help="Filter: exact machine partition key.")
@click.option("--since", default=None, help="Inclusive lower time bound (ISO-8601). Also bounds the rate window.")
@click.option("--until", default=None, help="Exclusive upper time bound (ISO-8601). Also bounds the rate window.")
@click.option(
    "--by",
    "group_by",
    type=click.Choice(["type", "source"]),
    default="type",
    show_default=True,
    help="Group the counts breakdown by event type or source.",
)
@click.option(
    "--rate-unit",
    type=click.Choice(list(_RATE_UNITS)),
    default="hour",
    show_default=True,
    help="Time unit for the event-rate figure.",
)
@click.option("--pair", is_flag=True, help="Also pair *.start/*.end events and summarize their durations.")
@_ledger_root_option
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text", show_default=True)
def stats_cmd(
    source: str | None,
    event_type: str | None,
    machine: str | None,
    since: str | None,
    until: str | None,
    group_by: str,
    rate_unit: str,
    pair: bool,
    repo_root: Path,
    ledger_root: Path | None,
    fmt: str,
) -> None:
    """Descriptive aggregations over matching events (counts / rate / durations)."""
    root = resolve_ledger_root(ledger_root, repo_root.resolve())
    store = LedgerStore(root=root)
    query = Query(
        source=source,
        type=event_type,
        machine=machine,
        since=since,
        until=until,
    )
    events = query_events(store.iter_events(), query)

    if group_by == "source":
        counts = counts_by_source(events)
    else:
        counts = counts_by_type(events)

    rate = event_rate(events, since=since, until=until)
    unit = _RATE_UNITS[rate_unit]
    per_unit = rate.per(unit)

    payload: dict[str, Any] = {
        "total": len(events),
        "by": group_by,
        "counts": counts,
        "rate": {
            "count": rate.count,
            "start": rate.start.isoformat() if rate.start is not None else None,
            "end": rate.end.isoformat() if rate.end is not None else None,
            "span_seconds": rate.span_seconds,
            "unit": rate_unit,
            "per_unit": per_unit,
        },
    }

    if pair:
        pairing = pair_events(events)
        durations = DurationStats.from_pairing(pairing)
        payload["durations"] = {
            "matched": durations.count,
            "unmatched_starts": len(pairing.unmatched_starts),
            "unmatched_ends": len(pairing.unmatched_ends),
            "total_seconds": durations.total_seconds,
            "min_seconds": durations.min_seconds,
            "max_seconds": durations.max_seconds,
            "mean_seconds": durations.mean_seconds,
            "median_seconds": durations.median_seconds,
        }

    if fmt == "json":
        click.echo(_json.dumps(payload, indent=2))
        return

    # text output
    click.echo(f"Events: {payload['total']}")
    click.echo(f"\nCounts by {group_by}:")
    if counts:
        for k in sorted(counts, key=lambda key: (-counts[key], key)):
            click.echo(f"  {counts[k]:>6}  {k}")
    else:
        click.echo("  (none)")

    click.echo(f"\nRate: {per_unit:.4g} events/{rate_unit}")
    click.echo(f"  count={rate.count}  span={rate.span_seconds:.0f}s")
    if rate.start is not None and rate.end is not None:
        click.echo(f"  observed {rate.start.isoformat()} → {rate.end.isoformat()}")

    if pair:
        d = payload["durations"]
        click.echo("\nPaired *.start/*.end durations:")
        click.echo(
            f"  matched={d['matched']}  unmatched_starts={d['unmatched_starts']}  "
            f"unmatched_ends={d['unmatched_ends']}"
        )
        if d["matched"]:
            click.echo(
                f"  total={d['total_seconds']:.0f}s  min={d['min_seconds']:.0f}s  "
                f"max={d['max_seconds']:.0f}s  mean={d['mean_seconds']:.1f}s  "
                f"median={d['median_seconds']:.1f}s"
            )


def _anomaly_to_dict(anomaly: Anomaly) -> dict[str, Any]:
    """Render one :class:`Anomaly` as a JSON-serializable dict."""
    out: dict[str, Any] = {
        "rule": anomaly.rule,
        "severity": anomaly.severity,
        "message": anomaly.message,
    }
    if anomaly.event is not None:
        out["event"] = {
            "time": anomaly.event.time,
            "type": anomaly.event.type,
            "source": anomaly.event.source,
            "id": anomaly.event.id,
        }
    if anomaly.detail:
        out["detail"] = anomaly.detail
    return out


def _digest_to_payload(report: Digest) -> dict[str, Any]:
    """Render a :class:`Digest` as the JSON output payload."""
    r = report.rollup
    return {
        "total": r.total,
        "counts": r.counts_by_type,
        "resources": {
            "shipped": r.resources.shipped,
            "tokens": r.resources.tokens,
            "cost": r.resources.cost,
            "duration_s": r.resources.duration_s,
        },
        "durations": {
            "matched": r.durations.count,
            "total_seconds": r.durations.total_seconds,
            "min_seconds": r.durations.min_seconds,
            "max_seconds": r.durations.max_seconds,
            "mean_seconds": r.durations.mean_seconds,
            "median_seconds": r.durations.median_seconds,
        },
        "recent": {
            "decisions": [
                {"time": n.time, "source": n.source, "summary": n.summary}
                for n in r.recent_decisions
            ],
            "failures": [
                {"time": n.time, "source": n.source, "summary": n.summary}
                for n in r.recent_failures
            ],
            "refusals": [
                {"time": n.time, "source": n.source, "summary": n.summary}
                for n in r.recent_refusals
            ],
        },
        "anomalies": [_anomaly_to_dict(a) for a in report.anomalies],
    }


def _echo_digest_text(report: Digest) -> None:
    """Print a :class:`Digest` as a human-readable text report."""
    r = report.rollup
    click.echo(f"Events: {r.total}")

    click.echo("\nCounts by type:")
    if r.counts_by_type:
        for k in sorted(r.counts_by_type, key=lambda key: (-r.counts_by_type[key], key)):
            click.echo(f"  {r.counts_by_type[k]:>6}  {k}")
    else:
        click.echo("  (none)")

    res = r.resources
    click.echo(
        f"\nResources (over {res.shipped} shipped): "
        f"tokens={res.tokens:g}  cost={res.cost:g}  duration={res.duration_s:g}s"
    )

    d = r.durations
    if d.count:
        click.echo(
            f"\nPaired durations: matched={d.count}  total={d.total_seconds:.0f}s  "
            f"mean={d.mean_seconds:.1f}s  median={d.median_seconds:.1f}s"
        )

    for label, notables in (
        ("Recent autonomous decisions", r.recent_decisions),
        ("Recent failures", r.recent_failures),
        ("Recent refusals", r.recent_refusals),
    ):
        if notables:
            click.echo(f"\n{label}:")
            for n in notables:
                click.echo(f"  {n.time}  {n.source}  {n.summary}")

    click.echo("\nAnomalies:")
    if report.anomalies:
        for a in report.anomalies:
            when = f"  ({a.event.time})" if a.event is not None else ""
            click.echo(f"  [{a.severity}] {a.rule}: {a.message}{when}")
    else:
        click.echo("  (none)")


@ledger_group.command(name="digest")
@click.option("--source", default=None, help="Filter: exact CloudEvents source.")
@click.option("--since", default=None, help="Inclusive lower time bound (ISO-8601, Z or offset).")
@click.option("--until", default=None, help="Exclusive upper time bound (ISO-8601, Z or offset).")
@click.option(
    "--spike-threshold",
    type=int,
    default=3,
    show_default=True,
    help="Failure count in the window that trips the failure-rate spike alert.",
)
@_ledger_root_option
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text", show_default=True)
def digest_cmd(
    source: str | None,
    since: str | None,
    until: str | None,
    spike_threshold: int,
    repo_root: Path,
    ledger_root: Path | None,
    fmt: str,
) -> None:
    """Deterministic oversight digest: rollup + rule-based anomaly flags.

    Loads the matching window via the public ledger query API and computes,
    purely over the event stream (no model), the rollup (counts / tokens / cost
    / durations / recent notables) plus the anomaly flags: could-have-asked
    decisions, failures and failure-rate spikes, push-without-green, token /
    duration outliers, and refusals.
    """
    root = resolve_ledger_root(ledger_root, repo_root.resolve())
    store = LedgerStore(root=root)
    query = Query(source=source, since=since, until=until)
    events = query_events(store.iter_events(), query)

    report = compute_digest(
        events, CLAUDE_DIGEST_CONFIG, spike_threshold=spike_threshold
    )

    if fmt == "json":
        click.echo(_json.dumps(_digest_to_payload(report), indent=2))
        return
    _echo_digest_text(report)


def _make_model_client(model: str | None) -> ModelClient:
    """Construct the real (Agent SDK) model client for the audit.

    Factored into a module-level function so it is the single seam tests
    override: a test monkeypatches this to return a fake client (no network) or
    to raise :class:`ModelUnavailableError` (the degrade-without-credential
    path). Raises :class:`ModelUnavailableError` when the SDK is not installed.
    """
    return AgentSdkModelClient(model=model)


def _audit_to_payload(
    result: AuditResult,
    *,
    appended: list[Any],
    fresh: list[CandidateEvent],
    dry_run: bool,
    sessions: int,
    files_read: int,
    turns: int,
    chunks: int,
) -> dict[str, Any]:
    """Render the audit outcome as the JSON output payload (the necessity report
    + the reconstructed-event deltas)."""
    return {
        "dry_run": dry_run,
        "scanned": {
            "sessions": sessions,
            "files_read": files_read,
            "turns": turns,
            "chunks": chunks,
        },
        "reconstructed": {
            # Candidates the model proposed, minus what's already on the ledger.
            "candidates": len(result.candidates),
            "novel": len(fresh),
            "appended": [_event_to_dict(e) for e in appended],
            "events": [
                {
                    "kind": c.kind,
                    "session_id": c.session_id,
                    "time": c.time,
                    "data": c.data,
                }
                for c in fresh
            ],
        },
        "necessity": [
            {
                "target_kind": n.target_kind,
                "label": n.label,
                "summary": n.summary,
                "rationale": n.rationale,
                "session_id": n.session_id,
            }
            for n in result.necessity
        ],
        "parse_errors": result.parse_errors,
    }


def _echo_audit_text(payload: dict[str, Any]) -> None:
    """Print the audit outcome as a human-readable text report."""
    scanned = payload["scanned"]
    click.echo(
        f"Scanned {scanned['sessions']} session(s) / {scanned['files_read']} "
        f"file(s): {scanned['turns']} turn(s) in {scanned['chunks']} chunk(s)."
    )

    rec = payload["reconstructed"]
    mode = "DRY-RUN (nothing written)" if payload["dry_run"] else "appended"
    click.echo(
        f"\nReconstructed events: {rec['candidates']} candidate(s), "
        f"{rec['novel']} novel after dedup ({mode})."
    )
    if rec["events"]:
        for e in rec["events"]:
            when = f"  ({e['time']})" if e["time"] else ""
            summary = e["data"].get("summary") or e["data"].get("action") or ""
            click.echo(f"  [{e['kind']}] {summary}{when}  session={e['session_id']}")
    else:
        click.echo("  (none)")

    click.echo("\nNecessity report:")
    if payload["necessity"]:
        for n in payload["necessity"]:
            click.echo(
                f"  [{n['label']}] {n['target_kind']}: {n['summary']}"
                + (f" — {n['rationale']}" if n["rationale"] else "")
            )
    else:
        click.echo("  (none)")

    if payload["parse_errors"]:
        click.echo(f"\n({payload['parse_errors']} response fragment(s) unparsed.)")


@ledger_group.command(name="audit")
@click.option("--since", default=None, help="Inclusive lower time bound for the transcript window (ISO-8601, Z or offset).")
@click.option("--until", default=None, help="Exclusive upper time bound for the transcript window (ISO-8601, Z or offset).")
@click.option("--session", "session_id", default=None, help="Audit only the transcript whose session id (filename stem) matches.")
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    show_default=True,
    help="Preview what would be appended without writing (default). Use --no-dry-run to append.",
)
@click.option(
    "--projects-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Session-transcript root. Default: $CLAUDE_PROJECTS_DIR, else ~/.claude/projects.",
)
@click.option(
    "--max-tokens",
    type=int,
    default=20_000,
    show_default=True,
    help="Per-chunk model token budget (transcript turns are packed into chunks of this size).",
)
@click.option("--model", default=None, help="Opaque model identifier passed through to the model client.")
@click.option("--machine", default=None, help="Machine partition key for appended events. Default: resolved machine id.")
@_ledger_root_option
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text", show_default=True)
def audit_cmd(
    since: str | None,
    until: str | None,
    session_id: str | None,
    dry_run: bool,
    projects_root: Path | None,
    max_tokens: int,
    model: str | None,
    machine: str | None,
    repo_root: Path,
    ledger_root: Path | None,
    fmt: str,
) -> None:
    """Model-powered oversight audit: reconstruct missed events + necessity report.

    Reads the Claude Code session transcripts for the window (optionally one
    ``--session``), runs an independent model pass over them, and reconstructs
    the ``decision.autonomous`` / ``failure`` / ``refusal`` events the live
    self-emit layer missed — each stamped with ``source=oversight-analyzer`` and
    ``data.reconstructed`` provenance, deduped against prior audit output by
    ``(session_id, signature)`` so re-auditing never double-emits. Also prints a
    needed-vs-rote necessity report over the interaction prompts it observed.

    ``--dry-run`` is the **default** and appends nothing; pass ``--no-dry-run``
    to append the reconstructed events best-effort. Append-only: never mutates
    or deletes existing events. When no model credential / SDK is available the
    command degrades to a clear no-op message rather than failing.
    """
    # 1. Load + chunk the transcript window (pure, model-free).
    load = load_transcripts(
        root=projects_root, since=since, until=until, session_id=session_id
    )
    sessions = len({t.session_id for t in load.turns})
    chunks = chunk_turns(load.turns, max_tokens=max_tokens) if load.turns else []

    if not chunks:
        click.echo("No transcript turns in the window; nothing to audit.")
        return

    # 2. Construct the model client. Absence of SDK/credential is a clean
    #    no-op, never a crash (the whim's "never blocks" invariant).
    try:
        client = _make_model_client(model)
    except ModelUnavailableError as exc:
        click.echo(f"No model credential available — audit skipped (no-op): {exc}")
        return

    # 3. Reconstruct over each chunk, then dedup against the existing ledger.
    #    A window may span multiple sessions; run per session so each finding's
    #    provenance + dedup key is scoped correctly.
    root = resolve_ledger_root(ledger_root, repo_root.resolve())
    store = LedgerStore(root=root)
    config = ReconstructConfig(model=model)
    existing = existing_signatures(store.read_all().events, config)

    by_session: dict[str, list[TranscriptChunk]] = {}
    for chunk in chunks:
        # All turns in a chunk share order; group chunks back to their sessions
        # by the (single) session of their first turn — chunk_turns preserves
        # the loader's per-session ordering.
        key = chunk.turns[0].session_id if chunk.turns else (session_id or "")
        by_session.setdefault(key, []).append(chunk)

    all_candidates: list[CandidateEvent] = []
    all_necessity: list[NecessityLabel] = []
    parse_errors = 0
    for sess, sess_chunks in by_session.items():
        result = reconstruct_chunks(sess_chunks, client, session_id=sess)
        all_candidates.extend(result.candidates)
        all_necessity.extend(result.necessity)
        parse_errors += result.parse_errors

    merged = AuditResult(
        candidates=all_candidates, necessity=all_necessity, parse_errors=parse_errors
    )
    fresh = dedup_candidates(merged.candidates, existing)

    # 4. Append best-effort (unless dry-run). One bad append must not sink the
    #    rest — append-only, never blocks.
    appended: list[Any] = []
    if not dry_run:
        machine_id = machine if machine is not None else resolve_machine_id()
        for candidate in fresh:
            try:
                event = to_ledger_event(candidate, config, machine=machine_id)
                appended.append(store.append(event))
            except Exception:  # noqa: BLE001 - best-effort: one bad append must not sink the run.
                continue

    payload = _audit_to_payload(
        merged,
        appended=appended,
        fresh=fresh,
        dry_run=dry_run,
        sessions=sessions,
        files_read=load.files_read,
        turns=len(load.turns),
        chunks=len(chunks),
    )

    if fmt == "json":
        click.echo(_json.dumps(payload, indent=2))
        return
    _echo_audit_text(payload)


@ledger_group.command(name="serve")
@click.option("--port", type=int, default=8765, show_default=True, help="Localhost TCP port to bind.")
@click.option("--no-open", "no_open", is_flag=True, help="Do not open the visualizer in a browser.")
@_ledger_root_option
def serve_cmd(
    port: int,
    no_open: bool,
    repo_root: Path,
    ledger_root: Path | None,
) -> None:
    """Serve a read-only local web visualizer over the ledger.

    Starts a stdlib ``http.server`` bound to localhost that serves a single-page
    app plus JSON APIs (``/api/events``, ``/api/spans``, ``/api/meta``) backed by
    the ledger query/derivation layers. Best-effort opens a browser (suppress
    with ``--no-open``). Never writes the ledger; Ctrl-C to stop.
    """
    # Imported lazily so the rest of the CLI never pays the http.server import
    # cost (and so the viz layer stays an optional, isolated consumer).
    from evledger.viz.server import serve

    root = resolve_ledger_root(ledger_root, repo_root.resolve())
    serve(root=root, port=port, open_browser=not no_open)
