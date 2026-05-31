"""Unit tests for :mod:`evledger.stats` — basic descriptive aggregations.

The stats layer is *pure* over an iterable of events (typically the output of
:func:`evledger.query_events`, but any iterable works). It writes
nothing and never mutates the frozen events.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import timedelta
from pathlib import Path

import pytest

from evledger import (
    DurationStats,
    LedgerEvent,
    PairedDuration,
    Pairing,
    count_by,
    counts_by_source,
    counts_by_type,
    event_rate,
    new_event,
    pair_events,
)


def _event(
    *,
    machine: str = "m",
    type: str = "t",
    source: str | None = None,
    time: str = "2026-05-29T14:30:15Z",
    data: object | None = None,
    seq: int | None = None,
    id: str = "0" * 32,
) -> LedgerEvent:
    """Build a fully-formed event with a deterministic id unless overridden."""
    ev = new_event(
        machine=machine,
        type=type,
        source=source if source is not None else f"/{machine}/sys",
        data=data,
        id=id,
        time=time,
    )
    return ev if seq is None else ev.with_seq(seq)


# --- counts ---------------------------------------------------------------


def test_counts_by_source_tallies_each_source() -> None:
    events = [
        _event(source="/a/git", type="t1", id="1" * 32),
        _event(source="/a/git", type="t2", id="2" * 32),
        _event(source="/a/mcp", type="t3", id="3" * 32),
    ]

    assert counts_by_source(events) == {"/a/git": 2, "/a/mcp": 1}


def test_counts_by_type_tallies_each_type() -> None:
    events = [
        _event(type="git.commit", id="1" * 32),
        _event(type="git.commit", id="2" * 32),
        _event(type="git.push", id="3" * 32),
    ]

    assert counts_by_type(events) == {"git.commit": 2, "git.push": 1}


def test_counts_on_empty_iterable_is_empty_dict() -> None:
    assert counts_by_source([]) == {}
    assert counts_by_type([]) == {}


def test_count_by_takes_an_arbitrary_key_function() -> None:
    events = [
        _event(machine="laptop", id="1" * 32),
        _event(machine="laptop", id="2" * 32),
        _event(machine="server", id="3" * 32),
    ]

    assert count_by(events, lambda e: e.machine) == {"laptop": 2, "server": 1}


def test_count_by_returns_a_plain_dict() -> None:
    result = count_by([_event()], lambda e: e.type)
    assert type(result) is dict


def test_counts_consume_a_one_shot_iterator() -> None:
    # A generator can only be walked once; the function must not need to re-walk.
    gen = (e for e in [_event(type="a", id="1" * 32), _event(type="a", id="2" * 32)])
    assert counts_by_type(gen) == {"a": 2}


# --- event rate -----------------------------------------------------------


def test_event_rate_over_observed_span() -> None:
    # 3 events spanning exactly 2 hours -> 1.5 events/hour.
    events = [
        _event(type="e", time="2026-01-01T00:00:00Z", id="1" * 32),
        _event(type="e", time="2026-01-01T01:00:00Z", id="2" * 32),
        _event(type="e", time="2026-01-01T02:00:00Z", id="3" * 32),
    ]

    rate = event_rate(events)

    assert rate.count == 3
    assert rate.span_seconds == pytest.approx(2 * 3600)
    assert rate.per(timedelta(hours=1)) == pytest.approx(1.5)


def test_event_rate_per_minute() -> None:
    events = [
        _event(type="e", time="2026-01-01T00:00:00Z", id="1" * 32),
        _event(type="e", time="2026-01-01T00:10:00Z", id="2" * 32),
    ]

    rate = event_rate(events)

    # 2 events over a 600s span -> per-minute = 2 / (600/60) = 0.2
    assert rate.per(timedelta(minutes=1)) == pytest.approx(0.2)


def test_event_rate_uses_explicit_window_bounds_when_given() -> None:
    # The window (since/until) defines the denominator, not the observed extent.
    events = [
        _event(type="e", time="2026-01-01T00:30:00Z", id="1" * 32),
        _event(type="e", time="2026-01-01T00:45:00Z", id="2" * 32),
    ]

    rate = event_rate(
        events,
        since="2026-01-01T00:00:00Z",
        until="2026-01-01T01:00:00Z",
    )

    assert rate.count == 2
    assert rate.span_seconds == pytest.approx(3600)
    assert rate.per(timedelta(hours=1)) == pytest.approx(2.0)


def test_event_rate_accepts_datetime_window_bounds() -> None:
    from datetime import datetime, timezone

    events = [_event(type="e", time="2026-01-01T00:30:00Z", id="1" * 32)]

    rate = event_rate(
        events,
        since=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        until=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
    )

    assert rate.per(timedelta(hours=1)) == pytest.approx(1.0)


def test_event_rate_empty_is_zero_with_no_span() -> None:
    rate = event_rate([])

    assert rate.count == 0
    assert rate.span_seconds == 0.0
    assert rate.start is None
    assert rate.end is None
    # No observed span and no events -> rate is 0, not a division error.
    assert rate.per(timedelta(hours=1)) == 0.0


def test_event_rate_single_event_zero_observed_span_is_zero_rate() -> None:
    # One event has no observed span; with no explicit window the rate is 0.
    rate = event_rate([_event(time="2026-01-01T00:00:00Z")])

    assert rate.count == 1
    assert rate.span_seconds == 0.0
    assert rate.per(timedelta(hours=1)) == 0.0


def test_event_rate_single_event_with_explicit_window_is_nonzero() -> None:
    rate = event_rate(
        [_event(time="2026-01-01T00:30:00Z")],
        since="2026-01-01T00:00:00Z",
        until="2026-01-01T01:00:00Z",
    )

    assert rate.per(timedelta(hours=1)) == pytest.approx(1.0)


def test_event_rate_rejects_non_positive_unit() -> None:
    rate = event_rate([_event()])
    with pytest.raises(ValueError):
        rate.per(timedelta(0))


# --- paired durations -----------------------------------------------------


def test_pair_events_matches_start_to_end_within_scope() -> None:
    events = [
        _event(type="dev.x.mission.start", time="2026-01-01T00:00:00Z", id="1" * 32),
        _event(type="dev.x.mission.end", time="2026-01-01T00:30:00Z", id="2" * 32),
    ]

    pairing = pair_events(events)

    assert len(pairing.durations) == 1
    d = pairing.durations[0]
    assert d.base == "dev.x.mission"
    assert d.seconds == pytest.approx(1800)
    assert pairing.unmatched_starts == ()
    assert pairing.unmatched_ends == ()


def test_pair_events_is_fifo_within_a_base_type() -> None:
    # Two overlapping missions of the same base, no correlation key -> FIFO:
    # first start pairs with first end.
    events = [
        _event(type="job.start", time="2026-01-01T00:00:00Z", id="1" * 32),
        _event(type="job.start", time="2026-01-01T00:05:00Z", id="2" * 32),
        _event(type="job.end", time="2026-01-01T00:10:00Z", id="3" * 32),
        _event(type="job.end", time="2026-01-01T00:20:00Z", id="4" * 32),
    ]

    pairing = pair_events(events)

    secs = sorted(d.seconds for d in pairing.durations)
    # start@00:00 -> end@00:10 = 600s ; start@00:05 -> end@00:20 = 900s
    assert secs == pytest.approx([600, 900])


def test_pair_events_scopes_by_correlation_key() -> None:
    # With a key, a start pairs only with an end carrying the same key.
    events = [
        _event(type="job.start", data={"id": "A"}, time="2026-01-01T00:00:00Z", id="1" * 32),
        _event(type="job.start", data={"id": "B"}, time="2026-01-01T00:01:00Z", id="2" * 32),
        _event(type="job.end", data={"id": "B"}, time="2026-01-01T00:02:00Z", id="3" * 32),
        _event(type="job.end", data={"id": "A"}, time="2026-01-01T00:10:00Z", id="4" * 32),
    ]

    pairing = pair_events(events, key=lambda e: (e.data or {}).get("id"))

    by_seconds = {round(d.seconds): d for d in pairing.durations}
    # A: 00:00 -> 00:10 = 600s ; B: 00:01 -> 00:02 = 60s
    assert sorted(by_seconds) == [60, 600]


def test_pair_events_reports_unmatched_start() -> None:
    events = [
        _event(type="job.start", time="2026-01-01T00:00:00Z", id="1" * 32),
    ]

    pairing = pair_events(events)

    assert pairing.durations == ()
    assert [e.id for e in pairing.unmatched_starts] == ["1" * 32]
    assert pairing.unmatched_ends == ()


def test_pair_events_reports_unmatched_end() -> None:
    # An end with no preceding start in scope is unmatched (not negative).
    events = [
        _event(type="job.end", time="2026-01-01T00:00:00Z", id="1" * 32),
    ]

    pairing = pair_events(events)

    assert pairing.durations == ()
    assert pairing.unmatched_starts == ()
    assert [e.id for e in pairing.unmatched_ends] == ["1" * 32]


def test_pair_events_ignores_events_without_the_suffixes() -> None:
    events = [
        _event(type="job.start", time="2026-01-01T00:00:00Z", id="1" * 32),
        _event(type="git.commit", time="2026-01-01T00:01:00Z", id="2" * 32),
        _event(type="job.end", time="2026-01-01T00:02:00Z", id="3" * 32),
    ]

    pairing = pair_events(events)

    assert len(pairing.durations) == 1
    assert pairing.durations[0].seconds == pytest.approx(120)


def test_pair_events_custom_suffixes() -> None:
    events = [
        _event(type="span.open", time="2026-01-01T00:00:00Z", id="1" * 32),
        _event(type="span.close", time="2026-01-01T00:00:30Z", id="2" * 32),
    ]

    pairing = pair_events(events, start_suffix=".open", end_suffix=".close")

    assert len(pairing.durations) == 1
    assert pairing.durations[0].base == "span"
    assert pairing.durations[0].seconds == pytest.approx(30)


def test_pair_events_orders_by_time_regardless_of_input_order() -> None:
    # Feed end before start (out of order); pairing must sort by instant first.
    events = [
        _event(type="job.end", time="2026-01-01T00:10:00Z", id="2" * 32),
        _event(type="job.start", time="2026-01-01T00:00:00Z", id="1" * 32),
    ]

    pairing = pair_events(events)

    assert len(pairing.durations) == 1
    assert pairing.durations[0].seconds == pytest.approx(600)


def test_paired_duration_carries_endpoints() -> None:
    start = _event(type="job.start", time="2026-01-01T00:00:00Z", id="1" * 32)
    end = _event(type="job.end", time="2026-01-01T00:05:00Z", id="2" * 32)

    pairing = pair_events([start, end])
    d = pairing.durations[0]

    assert isinstance(d, PairedDuration)
    assert d.start.id == "1" * 32
    assert d.end.id == "2" * 32


# --- duration summary stats -----------------------------------------------


def test_duration_stats_summarizes_a_pairing() -> None:
    events = [
        _event(type="job.start", time="2026-01-01T00:00:00Z", id="1" * 32),
        _event(type="job.end", time="2026-01-01T00:01:00Z", id="2" * 32),  # 60s
        _event(type="job.start", time="2026-01-01T01:00:00Z", id="3" * 32),
        _event(type="job.end", time="2026-01-01T01:03:00Z", id="4" * 32),  # 180s
        _event(type="job.start", time="2026-01-01T02:00:00Z", id="5" * 32),
        _event(type="job.end", time="2026-01-01T02:02:00Z", id="6" * 32),  # 120s
    ]

    pairing = pair_events(events)
    stats = DurationStats.from_pairing(pairing)

    assert stats.count == 3
    assert stats.total_seconds == pytest.approx(360)
    assert stats.min_seconds == pytest.approx(60)
    assert stats.max_seconds == pytest.approx(180)
    assert stats.mean_seconds == pytest.approx(120)
    assert stats.median_seconds == pytest.approx(120)


def test_duration_stats_median_even_count_averages_middle_two() -> None:
    events = [
        _event(type="j.start", time="2026-01-01T00:00:00Z", id="1" * 32),
        _event(type="j.end", time="2026-01-01T00:00:10Z", id="2" * 32),  # 10s
        _event(type="j.start", time="2026-01-01T01:00:00Z", id="3" * 32),
        _event(type="j.end", time="2026-01-01T01:00:30Z", id="4" * 32),  # 30s
    ]

    stats = DurationStats.from_pairing(pair_events(events))

    assert stats.median_seconds == pytest.approx(20)  # (10 + 30) / 2


def test_duration_stats_empty_pairing_is_all_zero_count_zero() -> None:
    stats = DurationStats.from_pairing(pair_events([]))

    assert stats.count == 0
    assert stats.total_seconds == 0.0
    assert stats.min_seconds is None
    assert stats.max_seconds is None
    assert stats.mean_seconds is None
    assert stats.median_seconds is None


def test_duration_stats_from_seconds_directly() -> None:
    stats = DurationStats.from_seconds([5.0, 1.0, 3.0])

    assert stats.count == 3
    assert stats.min_seconds == pytest.approx(1.0)
    assert stats.max_seconds == pytest.approx(5.0)
    assert stats.median_seconds == pytest.approx(3.0)


# --- purity / value objects -----------------------------------------------


def test_stats_results_are_frozen() -> None:
    rate = event_rate([_event()])
    with pytest.raises(FrozenInstanceError):
        rate.count = 99  # type: ignore[misc]

    pairing = Pairing(durations=(), unmatched_starts=(), unmatched_ends=())
    with pytest.raises(FrozenInstanceError):
        pairing.durations = ()  # type: ignore[misc]


def test_stats_do_not_mutate_source_events() -> None:
    events = [
        _event(type="job.start", time="2026-01-01T00:00:00Z", id="1" * 32),
        _event(type="job.end", time="2026-01-01T00:01:00Z", id="2" * 32),
    ]
    before = list(events)

    counts_by_type(events)
    event_rate(events)
    pair_events(events)

    assert events == before


def test_pairing_round_trips_from_a_real_store(tmp_path: Path) -> None:
    # End-to-end: stats run over a real on-disk store's iterator.
    from evledger import LedgerStore

    store = LedgerStore(root=tmp_path)
    store.append(_event(type="job.start", time="2026-01-01T00:00:00Z", id="1" * 32))
    store.append(_event(type="job.end", time="2026-01-01T00:00:45Z", id="2" * 32))

    stats = DurationStats.from_pairing(pair_events(store.iter_events()))

    assert stats.count == 1
    assert stats.total_seconds == pytest.approx(45)
