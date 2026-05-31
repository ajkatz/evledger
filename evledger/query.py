"""Pure filter / lookup API over a stream of ledger events.

This module is the **query path** of the ledger (the ``ledger-query`` task of
the ``universal-event-ledger`` whim). It is a *pure* function over any iterable
of :class:`~evledger.schema.LedgerEvent` — typically
:meth:`~evledger.store.LedgerStore.iter_events`, but any iterable works.
It performs **no new persistence**: nothing is written, the source iterator is
consumed once, and the input events (frozen dataclasses) are never mutated.

Like the rest of the ledger it is standalone: it imports only the standard
library and the sibling :mod:`~evledger.schema` layer (for
:func:`~evledger.schema.parse_time`), nothing from elsewhere in
``claude_kg`` (Decision 9). There are no hardcoded paths or claude-specific
conventions — the caller supplies the event stream and the filter criteria.

A :class:`Query` is a frozen value object describing the filter. All populated
criteria are **ANDed** together; an empty :class:`Query` matches everything.
Supported criteria:

* ``source`` — exact match on the CloudEvents ``source`` URI-reference.
* ``type`` — shell-style glob (:mod:`fnmatch`) against the event ``type``, so
  ``"dev.x.mission.*"`` matches both ``...start`` and ``...end``. The glob is
  anchored to the whole string (``"mission"`` does not match
  ``"dev.x.mission.start"``).
* ``machine`` — exact match on the ``machine`` partition key.
* ``since`` — inclusive lower time bound (``event.time >= since``).
* ``until`` — exclusive upper time bound (``event.time < until``).

Time bounds accept either an ISO-8601 string (``Z`` suffix or numeric offset)
or an aware :class:`datetime.datetime`; comparisons are done on parsed UTC
instants, so events recorded with different textual offsets compare correctly.

Results are returned as a list ordered by ``(time, seq)`` — chronological by
parsed instant, ties broken by the per-machine monotonic ``seq``. Events with
no assigned ``seq`` (``None``, e.g. freeform events not yet stored) sort before
events that carry a ``seq`` at the same instant.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatchcase

from evledger.schema import LedgerEvent, parse_time

#: Internal type alias: a time bound may be an ISO string or an aware datetime.
TimeBound = str | datetime


@dataclass(frozen=True)
class Query:
    """An immutable description of a ledger filter.

    All populated criteria are combined with logical AND. An empty
    :class:`Query` (the default) matches every event. Pass it to
    :func:`query_events` together with an iterable of events.

    Attributes:
        source: Exact match on the event ``source``. ``None`` = no filter.
        type: Shell-style glob (:mod:`fnmatch`, case-sensitive, whole-string
            anchored) matched against the event ``type``. ``None`` = no filter.
        machine: Exact match on the event ``machine`` partition key.
            ``None`` = no filter.
        since: Inclusive lower time bound — an ISO-8601 string (``Z`` or
            offset) or an aware :class:`datetime.datetime`. ``None`` = no
            lower bound.
        until: Exclusive upper time bound, same accepted forms as ``since``.
            ``None`` = no upper bound.
    """

    source: str | None = None
    type: str | None = None
    machine: str | None = None
    since: TimeBound | None = None
    until: TimeBound | None = None


def _as_instant(bound: TimeBound) -> datetime:
    """Normalize a time bound to an aware UTC :class:`datetime`.

    Accepts an ISO-8601 string (delegating to
    :func:`~evledger.schema.parse_time`) or an already-aware
    :class:`datetime.datetime` (converted to UTC). A naive datetime is
    rejected so comparisons are never ambiguous.

    Raises:
        ValueError: if ``bound`` is a naive datetime.
    """
    if isinstance(bound, datetime):
        if bound.tzinfo is None:
            raise ValueError("time bound datetime must be timezone-aware")
        return parse_time(bound.isoformat())
    return parse_time(bound)


def _matches(event: LedgerEvent, query: Query, since: datetime | None,
             until: datetime | None) -> bool:
    """Return whether ``event`` satisfies every populated criterion of ``query``.

    ``since`` / ``until`` are the pre-parsed bounds (parsing the bound once for
    the whole scan rather than per event).
    """
    if query.source is not None and event.source != query.source:
        return False
    if query.type is not None and not fnmatchcase(event.type, query.type):
        return False
    if query.machine is not None and event.machine != query.machine:
        return False
    if since is not None or until is not None:
        instant = parse_time(event.time)
        if since is not None and instant < since:
            return False
        if until is not None and instant >= until:
            return False
    return True


def _sort_key(event: LedgerEvent) -> tuple[datetime, int]:
    """Order key: parsed instant, then ``seq`` (``None`` sorts first)."""
    # -1 places seq=None ahead of any assigned seq (which starts at 0) at the
    # same instant, giving a total, deterministic order without crashing on None.
    return (parse_time(event.time), event.seq if event.seq is not None else -1)


def query_events(events: Iterable[LedgerEvent], query: Query) -> list[LedgerEvent]:
    """Filter ``events`` by ``query`` and return them ordered by ``(time, seq)``.

    This is a pure function: it reads the iterable once, writes nothing, and
    does not mutate the (frozen) events. Pass
    :meth:`~evledger.store.LedgerStore.iter_events` for a stored ledger,
    or any in-memory iterable of :class:`~evledger.schema.LedgerEvent`.

    Args:
        events: Any iterable of events to filter.
        query: The :class:`Query` describing the filter (empty = match all).

    Returns:
        A new list of the matching events, sorted chronologically by parsed
        UTC instant with ties broken by per-machine ``seq``.
    """
    since = _as_instant(query.since) if query.since is not None else None
    until = _as_instant(query.until) if query.until is not None else None
    matched = [e for e in events if _matches(e, query, since, until)]
    matched.sort(key=_sort_key)
    return matched
