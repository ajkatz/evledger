"""Basic descriptive aggregations over a stream of ledger events.

This module is the **stats path** of the ledger (the ``ledger-stats`` task of
the ``universal-event-ledger`` whim). Like the query layer it is *pure*: every
function takes any iterable of :class:`~evledger.schema.LedgerEvent`
(typically the output of :func:`~evledger.query.query_events`, but any
iterable works), reads it once, writes nothing, and never mutates the frozen
events. It performs **no new persistence**.

It provides only **descriptive** statistics — counts, rates, and paired-event
durations. There is deliberately *no* inference: no prediction, anomaly
detection, or trend modeling. That analytical layer is a separate, deferred
task (``ledger-inference``); keeping it out here preserves the hard standalone
boundary (Decision 9) and a tiny, dependency-free surface.

Three families of aggregation:

* **Counts** — :func:`count_by` and the :func:`counts_by_source` /
  :func:`counts_by_type` convenience wrappers tally events by a key, returning
  a plain ``dict[str, int]``.
* **Rate** — :func:`event_rate` measures how many events occur per unit time.
  The denominator is either the observed extent of the events or an explicit
  ``since`` / ``until`` window. The :class:`EventRate` result exposes
  :meth:`EventRate.per` to render the rate against any :class:`datetime.timedelta`
  unit (per second, minute, hour, day, ...).
* **Durations** — :func:`pair_events` matches ``*.start`` events to their
  ``*.end`` partners (the ledger's point-in-time substitute for a ``duration``
  field) and :class:`DurationStats` summarizes the resulting spans.

Standalone: imports only the standard library and the sibling
:mod:`~evledger.schema` layer (for :func:`~evledger.schema.parse_time`).
No hardcoded paths, no claude-specific event taxonomy — the caller supplies the
event stream, the key/suffix conventions, and any window bounds.
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from evledger.schema import LedgerEvent, parse_time

#: A time bound may be an ISO-8601 string or an aware :class:`datetime.datetime`.
TimeBound = str | datetime


# --- counts ---------------------------------------------------------------


def count_by(
    events: Iterable[LedgerEvent], key: Callable[[LedgerEvent], Hashable]
) -> dict[Hashable, int]:
    """Tally ``events`` by an arbitrary key, returning a plain ``dict``.

    Pure over any (one-shot) iterable. The key function maps each event to a
    hashable bucket; the result maps each distinct bucket to its event count.

    Args:
        events: Any iterable of events.
        key: A function from an event to its (hashable) bucket.

    Returns:
        A plain ``dict`` mapping each bucket to its count.
    """
    return dict(Counter(key(e) for e in events))


def counts_by_source(events: Iterable[LedgerEvent]) -> dict[str, int]:
    """Count events grouped by their CloudEvents ``source``."""
    return count_by(events, lambda e: e.source)  # type: ignore[return-value]


def counts_by_type(events: Iterable[LedgerEvent]) -> dict[str, int]:
    """Count events grouped by their ``type``."""
    return count_by(events, lambda e: e.type)  # type: ignore[return-value]


# --- event rate -----------------------------------------------------------


def _as_instant(bound: TimeBound) -> datetime:
    """Normalize a time bound to an aware UTC :class:`datetime`.

    Accepts an ISO-8601 string (delegating to
    :func:`~evledger.schema.parse_time`) or an already-aware
    :class:`datetime.datetime`. A naive datetime is rejected.

    Raises:
        ValueError: if ``bound`` is a naive datetime.
    """
    if isinstance(bound, datetime):
        if bound.tzinfo is None:
            raise ValueError("time bound datetime must be timezone-aware")
        return parse_time(bound.isoformat())
    return parse_time(bound)


@dataclass(frozen=True)
class EventRate:
    """The rate of events over a measured span.

    Attributes:
        count: The number of events observed.
        start: The earliest event instant, or ``None`` when ``count == 0``.
        end: The latest event instant, or ``None`` when ``count == 0``.
        span_seconds: The denominator span in seconds. When an explicit window
            (``since`` / ``until``) was given to :func:`event_rate` this is the
            window width; otherwise it is ``end - start`` of the observed
            events (``0.0`` for zero or one event with no explicit window).
    """

    count: int
    start: datetime | None
    end: datetime | None
    span_seconds: float

    def per(self, unit: timedelta) -> float:
        """Render the rate as events per ``unit`` of time.

        For example ``per(timedelta(hours=1))`` gives events/hour. When the
        span is zero (no events, a single event with no explicit window, or a
        degenerate window) the rate is ``0.0`` rather than a division error.

        Args:
            unit: A positive :class:`datetime.timedelta` denominator unit.

        Returns:
            ``count / (span_seconds / unit_seconds)`` as a float, or ``0.0``
            when ``span_seconds`` is ``0``.

        Raises:
            ValueError: if ``unit`` is not strictly positive.
        """
        unit_seconds = unit.total_seconds()
        if unit_seconds <= 0:
            raise ValueError("rate unit must be a positive timedelta")
        if self.span_seconds <= 0:
            return 0.0
        return self.count / (self.span_seconds / unit_seconds)


def event_rate(
    events: Iterable[LedgerEvent],
    *,
    since: TimeBound | None = None,
    until: TimeBound | None = None,
) -> EventRate:
    """Compute the rate of ``events`` over a span.

    The span (the rate denominator) is determined as follows:

    * If both ``since`` and ``until`` are given, the span is the width of that
      explicit window — the events define only the numerator (their count).
    * Otherwise the span is the observed extent: the latest event instant minus
      the earliest. With zero or one event and no explicit window the span is
      ``0`` and :meth:`EventRate.per` returns ``0.0``.

    A partial window (only ``since`` *or* only ``until``) substitutes that bound
    for the corresponding observed extreme.

    Args:
        events: Any iterable of events to measure.
        since: Optional explicit lower bound (ISO string or aware datetime).
        until: Optional explicit upper bound (ISO string or aware datetime).

    Returns:
        An :class:`EventRate` describing the count, observed start/end, and
        span.
    """
    instants = sorted(parse_time(e.time) for e in events)
    count = len(instants)

    obs_start = instants[0] if instants else None
    obs_end = instants[-1] if instants else None

    win_start = _as_instant(since) if since is not None else obs_start
    win_end = _as_instant(until) if until is not None else obs_end

    if win_start is not None and win_end is not None:
        span = max(0.0, (win_end - win_start).total_seconds())
    else:
        span = 0.0

    return EventRate(count=count, start=obs_start, end=obs_end, span_seconds=span)


# --- paired durations -----------------------------------------------------


@dataclass(frozen=True)
class PairedDuration:
    """A matched ``*.start`` / ``*.end`` pair and its elapsed duration.

    Attributes:
        base: The shared type with the start/end suffix stripped, e.g.
            ``"dev.x.mission"`` for ``dev.x.mission.start`` /
            ``dev.x.mission.end``.
        key: The correlation key the pair was matched under (the value returned
            by the ``key`` function passed to :func:`pair_events`; ``None`` by
            default).
        start: The start event.
        end: The end event.
        seconds: The elapsed time ``end - start`` in seconds (``>= 0`` because
            events are matched in time order).
    """

    base: str
    key: Hashable
    start: LedgerEvent
    end: LedgerEvent
    seconds: float


@dataclass(frozen=True)
class Pairing:
    """The result of pairing start/end events.

    Attributes:
        durations: The matched pairs, ordered by start instant.
        unmatched_starts: Start events with no later end in their scope,
            ordered by instant.
        unmatched_ends: End events with no earlier unmatched start in their
            scope, ordered by instant.
    """

    durations: tuple[PairedDuration, ...]
    unmatched_starts: tuple[LedgerEvent, ...]
    unmatched_ends: tuple[LedgerEvent, ...]


def _default_key(_event: LedgerEvent) -> Hashable:
    """Default correlation key: a single global scope per base type."""
    return None


def pair_events(
    events: Iterable[LedgerEvent],
    *,
    key: Callable[[LedgerEvent], Hashable] = _default_key,
    start_suffix: str = ".start",
    end_suffix: str = ".end",
) -> Pairing:
    """Pair ``*.start`` events with their ``*.end`` partners.

    Events are first ordered by instant (ties broken by ``seq``). An event is a
    *start* if its ``type`` ends with ``start_suffix`` and an *end* if it ends
    with ``end_suffix``; the **base** is the ``type`` with that suffix removed.
    Events matching neither suffix are ignored.

    Within each ``(base, key(event))`` scope, starts are matched to ends
    first-in-first-out: each end closes the earliest still-open start of the
    same base and key. This pairs cleanly whether scopes are distinguished by a
    correlation ``key`` (e.g. ``data["id"]``) or left to the default single
    per-base scope (overlapping same-base spans then pair by arrival order).

    Args:
        events: Any iterable of events.
        key: Maps an event to its correlation scope (hashable). Defaults to a
            single global scope per base type.
        start_suffix: The ``type`` suffix marking a start event.
        end_suffix: The ``type`` suffix marking an end event.

    Returns:
        A :class:`Pairing` with the matched ``durations`` plus any
        ``unmatched_starts`` / ``unmatched_ends``, all ordered by instant.
    """
    ordered = sorted(
        events,
        key=lambda e: (parse_time(e.time), e.seq if e.seq is not None else -1),
    )

    # Open starts per (base, key), FIFO. Each entry is (instant, event).
    open_starts: dict[tuple[str, Hashable], deque[tuple[datetime, LedgerEvent]]] = (
        defaultdict(deque)
    )
    durations: list[PairedDuration] = []
    unmatched_ends: list[LedgerEvent] = []

    for event in ordered:
        if event.type.endswith(start_suffix):
            base = event.type[: -len(start_suffix)]
            instant = parse_time(event.time)
            open_starts[(base, key(event))].append((instant, event))
        elif event.type.endswith(end_suffix):
            base = event.type[: -len(end_suffix)]
            scope = (base, key(event))
            queue = open_starts.get(scope)
            if queue:
                start_instant, start_event = queue.popleft()
                end_instant = parse_time(event.time)
                durations.append(
                    PairedDuration(
                        base=base,
                        key=key(event),
                        start=start_event,
                        end=event,
                        seconds=(end_instant - start_instant).total_seconds(),
                    )
                )
            else:
                unmatched_ends.append(event)

    unmatched_starts = [
        ev for queue in open_starts.values() for _instant, ev in queue
    ]
    unmatched_starts.sort(
        key=lambda e: (parse_time(e.time), e.seq if e.seq is not None else -1)
    )
    durations.sort(key=lambda d: parse_time(d.start.time))

    return Pairing(
        durations=tuple(durations),
        unmatched_starts=tuple(unmatched_starts),
        unmatched_ends=tuple(unmatched_ends),
    )


# --- duration summary stats -----------------------------------------------


@dataclass(frozen=True)
class DurationStats:
    """Descriptive summary statistics for a set of durations (in seconds).

    Build from a :class:`Pairing` with :meth:`from_pairing`, or directly from a
    sequence of second-valued durations with :meth:`from_seconds`. When there
    are no durations, ``count`` is ``0``, ``total_seconds`` is ``0.0``, and the
    min/max/mean/median fields are ``None``.

    Attributes:
        count: The number of durations.
        total_seconds: The sum of all durations.
        min_seconds: The smallest duration, or ``None`` when empty.
        max_seconds: The largest duration, or ``None`` when empty.
        mean_seconds: The arithmetic mean, or ``None`` when empty.
        median_seconds: The median (averaging the middle two for an even
            count), or ``None`` when empty.
    """

    count: int
    total_seconds: float
    min_seconds: float | None
    max_seconds: float | None
    mean_seconds: float | None
    median_seconds: float | None

    @classmethod
    def from_seconds(cls, seconds: Iterable[float]) -> DurationStats:
        """Summarize an iterable of second-valued durations."""
        values = sorted(float(s) for s in seconds)
        count = len(values)
        if count == 0:
            return cls(
                count=0,
                total_seconds=0.0,
                min_seconds=None,
                max_seconds=None,
                mean_seconds=None,
                median_seconds=None,
            )
        total = sum(values)
        mid = count // 2
        if count % 2 == 1:
            median = values[mid]
        else:
            median = (values[mid - 1] + values[mid]) / 2
        return cls(
            count=count,
            total_seconds=total,
            min_seconds=values[0],
            max_seconds=values[-1],
            mean_seconds=total / count,
            median_seconds=median,
        )

    @classmethod
    def from_pairing(cls, pairing: Pairing) -> DurationStats:
        """Summarize the matched durations of a :class:`Pairing`."""
        return cls.from_seconds(d.seconds for d in pairing.durations)
