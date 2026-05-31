"""Pure derivation of spans, nesting, and link edges from ledger events.

This module is the **backend-agnostic derivation layer** of the ledger
visualizer (the ``viz-connections`` task of the ``ledger-viz`` whim). It is
*pure*: every function takes any iterable of
:class:`~evledger.LedgerEvent` (typically the output of
:func:`~evledger.query_events`, but any iterable works), reads it,
writes nothing, and never mutates the frozen events. It performs **no I/O and
no persistence**, and imports only the public :mod:`evledger` surface
plus the standard library — so the ``serve`` command (and any other consumer)
can call it without dragging in web or MCP dependencies.

Three derivations, all from inferred connectivity (no schema change):

* **Spans** — :func:`derive_spans` reuses
  :func:`~evledger.pair_events` to match ``*.start`` / ``*.end`` events
  into intervals. Each :class:`Span` carries the paired ``base`` type, its
  start/end instants, elapsed ``seconds``, and the contributing event ids.
* **Nesting** — every span gets a ``depth`` and ``parent`` assigned by **time
  containment**: span *B* nests under span *A* when *B*'s ``[start, end]``
  interval is a *strict* subset of *A*'s. The parent is the smallest such
  container; outermost spans are ``depth == 0`` with ``parent is None``.
  Overlapping (but non-containing) spans stay siblings.
* **Links** — :func:`derive_links` computes :class:`Link` edges between events
  from (a) each ``*.start`` ↔ ``*.end`` pair (``kind == "pair"``) and (b)
  events that share a ``data`` key/value (``kind == "data:<key>"``), e.g. two
  events both tagged ``{"whim": "ledger-viz"}``.

All results are **frozen value objects** with *tuple* fields (compare to
``()``, never ``[]``), consistent with the ledger core's immutability.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from evledger import LedgerEvent, pair_events, parse_time

__all__ = [
    "Span",
    "Link",
    "Connections",
    "derive_spans",
    "derive_links",
    "derive_connections",
]


@dataclass(frozen=True)
class Span:
    """A derived ``*.start`` / ``*.end`` interval with its nesting position.

    Attributes:
        id: A stable identifier for the span (the start event's id).
        base: The shared type with the start/end suffix stripped, e.g.
            ``"dev.x.mission"`` for ``dev.x.mission.start`` /
            ``dev.x.mission.end``.
        start: The start event's ``time`` (ISO-8601 ``Z``-suffixed string).
        end: The end event's ``time`` (ISO-8601 ``Z``-suffixed string).
        seconds: The elapsed time ``end - start`` in seconds (``>= 0``).
        depth: The nesting depth — ``0`` for an outermost span, ``1`` for a
            span whose smallest container is depth ``0``, and so on.
        parent: The ``id`` of the smallest span strictly containing this one,
            or ``None`` when this span is outermost.
        event_ids: The contributing event ids, ``(start_id, end_id)``.
    """

    id: str
    base: str
    start: str
    end: str
    seconds: float
    depth: int
    parent: str | None
    event_ids: tuple[str, ...]


@dataclass(frozen=True)
class Link:
    """A directed link edge between two events.

    Attributes:
        from_event_id: The id of the source event.
        to_event_id: The id of the target event.
        kind: The relationship kind. ``"pair"`` for a ``*.start`` ↔ ``*.end``
            pairing (``from`` is the start, ``to`` is the end);
            ``"data:<key>"`` for two events sharing the same ``data[<key>]``
            value.
    """

    from_event_id: str
    to_event_id: str
    kind: str


@dataclass(frozen=True)
class Connections:
    """The full derivation result: nested spans plus link edges.

    Attributes:
        spans: The derived spans, ordered by start instant (ties by id), each
            with its ``depth`` / ``parent`` nesting assigned.
        links: The link edges (pairing + shared-data), in derivation order.
    """

    spans: tuple[Span, ...]
    links: tuple[Link, ...]


def _strictly_contains(
    outer: tuple[datetime, datetime], inner: tuple[datetime, datetime]
) -> bool:
    """True when ``inner``'s interval is a *strict* subset of ``outer``'s.

    Strict means at least one bound is tighter and neither bound pokes out, so
    two identical intervals do **not** contain each other (no self-nesting).
    """
    o_start, o_end = outer
    i_start, i_end = inner
    return (
        o_start <= i_start
        and i_end <= o_end
        and (o_start < i_start or i_end < o_end)
    )


# Start/end suffix conventions recognized when pairing events into spans.
# The taxonomy uses both ``.start``/``.end`` (e.g. mission) and
# ``.started``/``.done`` (e.g. task); they are disjoint (".started" does not
# end with ".start"), so pairing each independently and merging is safe.
_PAIR_SUFFIXES: tuple[tuple[str, str], ...] = ((".start", ".end"), (".started", ".done"))


def derive_spans(events: Iterable[LedgerEvent]) -> tuple[Span, ...]:
    """Build nested spans from start/end event pairs.

    Reuses :func:`~evledger.pair_events` for the matching — once per
    recognized suffix convention in :data:`_PAIR_SUFFIXES` (``.start``/``.end``
    and ``.started``/``.done``), merging the results — then assigns each
    resulting span a ``depth`` and ``parent`` by time containment (the smallest
    strictly-containing span is the parent). Unpaired starts/ends contribute no
    span.

    Args:
        events: Any iterable of events.

    Returns:
        The spans ordered by start instant (ties broken by span id), as a
        tuple of frozen :class:`Span` objects.
    """
    materialized = list(events)
    durations = [
        dur
        for start_suffix, end_suffix in _PAIR_SUFFIXES
        for dur in pair_events(
            materialized, start_suffix=start_suffix, end_suffix=end_suffix
        ).durations
    ]

    # Build the bare spans first (no nesting yet), remembering each one's
    # parsed interval for the containment comparison.
    intervals: list[tuple[datetime, datetime]] = []
    bare: list[Span] = []
    for dur in durations:
        start_dt = parse_time(dur.start.time)
        end_dt = parse_time(dur.end.time)
        intervals.append((start_dt, end_dt))
        bare.append(
            Span(
                id=dur.start.id,
                base=dur.base,
                start=dur.start.time,
                end=dur.end.time,
                seconds=dur.seconds,
                depth=0,
                parent=None,
                event_ids=(dur.start.id, dur.end.id),
            )
        )

    # Assign parent = the smallest span strictly containing this one. "Smallest"
    # is the narrowest interval among all strict containers, which yields the
    # innermost ancestor (correct multi-level nesting).
    parents: list[str | None] = []
    for i, inner_iv in enumerate(intervals):
        best_idx: int | None = None
        best_width: float | None = None
        for j, outer_iv in enumerate(intervals):
            if i == j:
                continue
            if _strictly_contains(outer_iv, inner_iv):
                width = (outer_iv[1] - outer_iv[0]).total_seconds()
                if best_width is None or width < best_width:
                    best_width = width
                    best_idx = j
        parents.append(bare[best_idx].id if best_idx is not None else None)

    # Depth = number of ancestors walking parent pointers up to a root.
    id_to_index = {span.id: idx for idx, span in enumerate(bare)}

    def _depth(idx: int) -> int:
        d = 0
        seen: set[int] = set()
        cur = parents[idx]
        while cur is not None and cur in id_to_index and idx not in seen:
            seen.add(idx)
            d += 1
            idx = id_to_index[cur]
            cur = parents[idx]
        return d

    nested = [
        Span(
            id=span.id,
            base=span.base,
            start=span.start,
            end=span.end,
            seconds=span.seconds,
            depth=_depth(i),
            parent=parents[i],
            event_ids=span.event_ids,
        )
        for i, span in enumerate(bare)
    ]

    nested.sort(key=lambda s: (parse_time(s.start), s.id))
    return tuple(nested)


def derive_links(events: Iterable[LedgerEvent]) -> tuple[Link, ...]:
    """Compute link edges between events.

    Two families of edge are produced:

    * **Pairing** — each ``*.start`` ↔ ``*.end`` match (via
      :func:`~evledger.pair_events`) yields one ``kind == "pair"`` edge
      from the start event to the end event.
    * **Shared data** — events whose ``data`` is a mapping are grouped by each
      ``(key, value)`` they carry; within a group of two or more events
      (ordered by instant) consecutive events are chained with a
      ``kind == "data:<key>"`` edge. Non-mapping ``data`` (``None``, strings,
      lists) is ignored, and values that are not hashable are skipped.

    Args:
        events: Any iterable of events.

    Returns:
        The link edges as a tuple of frozen :class:`Link` objects: the pairing
        edges first, then the shared-data edges (each value-group chained in
        time order).
    """
    materialized = list(events)

    links: list[Link] = []

    # (a) start <-> end pairing edges, across all recognized suffix conventions.
    for start_suffix, end_suffix in _PAIR_SUFFIXES:
        for dur in pair_events(
            materialized, start_suffix=start_suffix, end_suffix=end_suffix
        ).durations:
            links.append(
                Link(from_event_id=dur.start.id, to_event_id=dur.end.id, kind="pair")
            )

    # (b) shared data key/value edges. Group event ids by (key, value), keeping
    # arrival (time) order so the chaining is deterministic.
    ordered = sorted(
        materialized,
        key=lambda e: (parse_time(e.time), e.seq if e.seq is not None else -1),
    )
    groups: dict[tuple[str, object], list[str]] = defaultdict(list)
    for event in ordered:
        data = event.data
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            if not isinstance(key, str):
                continue
            try:
                hash(value)
            except TypeError:
                continue
            groups[(key, value)].append(event.id)

    for (key, _value), ids in groups.items():
        for a, b in zip(ids, ids[1:]):
            links.append(Link(from_event_id=a, to_event_id=b, kind=f"data:{key}"))

    return tuple(links)


def derive_connections(events: Iterable[LedgerEvent]) -> Connections:
    """Derive both nested spans and link edges in one pass-friendly call.

    The events are materialized once and fed to both :func:`derive_spans` and
    :func:`derive_links`, so a one-shot iterable is consumed safely.

    Args:
        events: Any iterable of events.

    Returns:
        A :class:`Connections` bundling the nested ``spans`` and the ``links``.
    """
    materialized = list(events)
    return Connections(
        spans=derive_spans(materialized),
        links=derive_links(materialized),
    )
