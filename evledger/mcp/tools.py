"""SDK-free tool handlers for the ledger MCP server (the ``mcp-tools`` task).

These are pure-ish functions that an MCP server (the next ``mcp-server`` task)
binds as tools, but they carry **no dependency on the ``mcp`` SDK** — they import
only the public :mod:`evledger` API and the standard library. That keeps
this layer the testable core: it can be exercised directly against a temporary
ledger root without any MCP runtime.

Three handlers mirror the ``claude-kg ledger`` CLI surface:

* :func:`ledger_log` — append one CloudEvent, returning the stored event dict
  (``id`` / ``time`` / ``seq`` filled).
* :func:`ledger_query` — filter the ledger, returning a list of matching event
  dicts ordered by ``(time, seq)``.
* :func:`ledger_stats` — descriptive aggregations (counts / rate / optional
  paired durations) as a plain dict.

Every return value is a plain ``dict`` / ``list`` of JSON-serializable scalars,
so an MCP server can hand it straight back to a client. Inputs are coerced
defensively: ``data`` may arrive as a JSON string (MCP clients often pass
stringified JSON) or as an already-parsed object; ISO-8601 time bounds are
passed through to the ledger's own parsers (which accept ``Z`` or numeric
offsets).

Ledger-root resolution mirrors the CLI but defaults to ``<cwd>/ledger`` (this
layer has no ``--repo-root`` notion): explicit ``root`` argument →
``$CLAUDE_LEDGER_ROOT`` → ``<cwd>/ledger``. There is no hardcoded ``~/.claude``
path here; the only home-relative default lives in the machine-id resolver.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

from evledger import (
    DurationStats,
    LedgerStore,
    Query,
    counts_by_source,
    counts_by_type,
    event_rate,
    new_event,
    pair_events,
    query_events,
    resolve_machine_id,
)

#: Environment variable consulted for the ledger root when no explicit ``root``
#: argument is given. Matches the CLI (``evledger.cli``).
LEDGER_ROOT_ENV_VAR = "CLAUDE_LEDGER_ROOT"

#: Recognized ``rate_unit`` values mapped to their :class:`~datetime.timedelta`.
_RATE_UNITS: dict[str, timedelta] = {
    "second": timedelta(seconds=1),
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
}


def resolve_ledger_root(
    root: Path | str | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | str | None = None,
) -> Path:
    """Resolve the ledger *instance* root directory for the MCP layer.

    Resolution order (Decision 5, adapted for a server with no repo-root):

    1. The explicit ``root`` argument, if given.
    2. The ``$CLAUDE_LEDGER_ROOT`` environment variable, if set and non-blank.
    3. ``<cwd>/ledger`` as the generic default.

    No hardcoded ``~/.claude`` path: the only home-relative default lives in the
    machine-id resolver, not here.

    Args:
        root: An explicit ledger root (e.g. from a launcher ``--ledger-root``
            flag). ``None`` to fall through to the env var / default.
        env: Environment mapping to read ``$CLAUDE_LEDGER_ROOT`` from. Defaults
            to :data:`os.environ`.
        cwd: The working directory used to derive the ``<cwd>/ledger`` default.
            Defaults to the process current working directory.

    Returns:
        The resolved ledger root :class:`~pathlib.Path` (not necessarily
        existing — the store creates it on first append).
    """
    if root is not None:
        return Path(root)
    environ = os.environ if env is None else env
    env_value = environ.get(LEDGER_ROOT_ENV_VAR)
    if env_value is not None and env_value.strip():
        return Path(env_value)
    base = Path.cwd() if cwd is None else Path(cwd)
    return base / "ledger"


def _coerce_data(data: Any | None) -> Any | None:
    """Coerce a tool's ``data`` argument into a JSON value.

    MCP clients may pass the event payload either as an already-parsed object
    (dict/list/scalar) or as a JSON-encoded *string*. A string is parsed as
    JSON when it looks like JSON; if it is not valid JSON it is kept verbatim as
    a plain string payload (so ``data="hello"`` records the string ``"hello"``,
    not an error). ``None`` stays ``None`` (no payload).

    Raises:
        ValueError: never — invalid JSON strings degrade to the raw string.
    """
    if data is None:
        return None
    if isinstance(data, str):
        try:
            return json.loads(data)
        except (json.JSONDecodeError, ValueError):
            return data
    return data


def ledger_log(
    source: str,
    type: str,
    data: Any | None = None,
    machine: str | None = None,
    *,
    root: Path | str | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | str | None = None,
) -> dict[str, Any]:
    """Append one CloudEvent to the ledger and return the stored event.

    Builds the event with :func:`~evledger.new_event` (generating ``id``
    and ``time``) and appends it with
    :meth:`~evledger.LedgerStore.append` (which assigns the monotonic
    per-machine ``seq``).

    Args:
        source: CloudEvents ``source`` URI-reference (e.g. ``/<machine>/<sys>``).
        type: Reverse-DNS event ``type`` (e.g. ``dev.example.mission.start``).
        data: Optional JSON payload — an object, or a JSON-encoded string (a
            non-JSON string is stored verbatim). ``None`` = no payload.
        machine: Machine partition key. ``None`` resolves via
            :func:`~evledger.resolve_machine_id`.
        root: Explicit ledger root; else ``$CLAUDE_LEDGER_ROOT``; else
            ``<cwd>/ledger``.
        env: Environment mapping (root + machine-id resolution). Defaults to
            :data:`os.environ`.
        cwd: Working dir for the ``<cwd>/ledger`` default.

    Returns:
        The stored event as a CloudEvents :meth:`~evledger.LedgerEvent.to_dict`
        dict, with ``id`` / ``time`` / ``seq`` populated.
    """
    resolved_root = resolve_ledger_root(root, env=env, cwd=cwd)
    machine_id = machine if machine is not None else resolve_machine_id(env=env)
    payload = _coerce_data(data)

    event = new_event(source=source, type=type, machine=machine_id, data=payload)
    store = LedgerStore(root=resolved_root)
    stored = store.append(event)
    return stored.to_dict()


def ledger_query(
    source: str | None = None,
    type: str | None = None,
    machine: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
    *,
    root: Path | str | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | str | None = None,
) -> dict[str, Any]:
    """Query the ledger and return matching events ordered by ``(time, seq)``.

    Applies a :class:`~evledger.Query` over the store's events via the
    pure :func:`~evledger.query_events` filter. All populated criteria
    are ANDed; ``type`` accepts a shell glob; ``since`` is inclusive and
    ``until`` exclusive (ISO-8601, ``Z`` or numeric offset).

    Args:
        source: Exact ``source`` filter, or ``None``.
        type: ``type`` glob filter (e.g. ``dev.x.mission.*``), or ``None``.
        machine: Exact machine filter, or ``None``.
        since: Inclusive lower ISO-8601 time bound, or ``None``.
        until: Exclusive upper ISO-8601 time bound, or ``None``.
        limit: Cap on the number of events returned. The most-recent ``limit``
            events (last in ``(time, seq)`` order) are kept; ``0`` returns none;
            ``None`` / negative returns all.
        root: Explicit ledger root; else ``$CLAUDE_LEDGER_ROOT``; else
            ``<cwd>/ledger``.
        env: Environment mapping (root resolution). Defaults to
            :data:`os.environ`.
        cwd: Working dir for the ``<cwd>/ledger`` default.

    Returns:
        A dict ``{"count": int, "events": list[dict]}`` where ``count`` is the
        number of returned events and each event is a CloudEvents dict.
    """
    resolved_root = resolve_ledger_root(root, env=env, cwd=cwd)
    store = LedgerStore(root=resolved_root)
    query = Query(source=source, type=type, machine=machine, since=since, until=until)
    events = query_events(store.iter_events(), query)
    if limit is not None and limit >= 0:
        events = events[-limit:] if limit else []
    return {
        "count": len(events),
        "events": [e.to_dict() for e in events],
    }


def ledger_stats(
    source: str | None = None,
    type: str | None = None,
    machine: str | None = None,
    since: str | None = None,
    until: str | None = None,
    by: str = "type",
    rate_unit: str = "hour",
    pair: bool = False,
    *,
    root: Path | str | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | str | None = None,
) -> dict[str, Any]:
    """Compute descriptive aggregations over matching ledger events.

    Mirrors the ``claude-kg ledger stats`` JSON shape: counts (by type or
    source), an event rate over the observed/explicit window, and — with
    ``pair=True`` — a summary of paired ``*.start`` / ``*.end`` durations.

    Args:
        source: Exact ``source`` filter, or ``None``.
        type: ``type`` glob filter, or ``None``.
        machine: Exact machine filter, or ``None``.
        since: Inclusive lower ISO-8601 bound; also bounds the rate window.
        until: Exclusive upper ISO-8601 bound; also bounds the rate window.
        by: Counts breakdown key — ``"type"`` (default) or ``"source"``.
        rate_unit: Rate denominator unit — one of ``second`` / ``minute`` /
            ``hour`` (default) / ``day``.
        pair: When ``True``, also pair ``*.start`` / ``*.end`` events and
            summarize their durations under a ``"durations"`` key.
        root: Explicit ledger root; else ``$CLAUDE_LEDGER_ROOT``; else
            ``<cwd>/ledger``.
        env: Environment mapping (root resolution). Defaults to
            :data:`os.environ`.
        cwd: Working dir for the ``<cwd>/ledger`` default.

    Returns:
        A dict with ``total`` (matched count), ``by`` (the breakdown key),
        ``counts`` (``{key: count}``), and ``rate``
        (``{count, start, end, span_seconds, unit, per_unit}``). When ``pair``
        is set, also ``durations``
        (``{matched, unmatched_starts, unmatched_ends, total_seconds,
        min_seconds, max_seconds, mean_seconds, median_seconds}``).

    Raises:
        ValueError: if ``by`` or ``rate_unit`` is not a recognized value.
    """
    if by not in ("type", "source"):
        raise ValueError(f"by must be 'type' or 'source', got {by!r}")
    if rate_unit not in _RATE_UNITS:
        raise ValueError(
            f"rate_unit must be one of {sorted(_RATE_UNITS)}, got {rate_unit!r}"
        )

    resolved_root = resolve_ledger_root(root, env=env, cwd=cwd)
    store = LedgerStore(root=resolved_root)
    query = Query(source=source, type=type, machine=machine, since=since, until=until)
    events = query_events(store.iter_events(), query)

    counts = counts_by_source(events) if by == "source" else counts_by_type(events)

    rate = event_rate(events, since=since, until=until)
    per_unit = rate.per(_RATE_UNITS[rate_unit])

    payload: dict[str, Any] = {
        "total": len(events),
        "by": by,
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

    return payload
