"""Unit tests for :mod:`evledger.query` — the pure filter/lookup API."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from evledger import (
    LedgerEvent,
    LedgerStore,
    Query,
    new_event,
    query_events,
)


def _event(
    *,
    machine: str = "m",
    type: str = "t",
    source: str | None = None,
    time: str = "2026-05-29T14:30:15Z",
    seq: int | None = None,
    id: str = "0" * 32,
) -> LedgerEvent:
    """Build a fully-formed event with a deterministic id unless overridden."""
    ev = new_event(
        machine=machine,
        type=type,
        source=source if source is not None else f"/{machine}/sys",
        id=id,
        time=time,
    )
    return ev if seq is None else ev.with_seq(seq)


def _seed(tmp_path: Path, events: list[LedgerEvent]) -> LedgerStore:
    """Append the given events to a real store rooted at ``tmp_path``."""
    store = LedgerStore(root=tmp_path)
    for ev in events:
        store.append(ev)
    return store


# --- query over a store: no filter = everything, ordered ------------------


def test_empty_query_returns_all_events(tmp_path: Path) -> None:
    store = _seed(
        tmp_path,
        [
            _event(machine="a", type="x"),
            _event(machine="b", type="y"),
        ],
    )

    results = query_events(store.iter_events(), Query())

    assert len(results) == 2


def test_query_accepts_a_plain_iterable_not_just_a_store(tmp_path: Path) -> None:
    # The query is pure over *any* iterable of events, not coupled to the store.
    events = [
        _event(machine="a", type="x", seq=0),
        _event(machine="a", type="y", seq=1),
    ]

    results = query_events(events, Query(type="x"))

    assert [e.type for e in results] == ["x"]


# --- filter by source -----------------------------------------------------


def test_filter_by_source_exact_match(tmp_path: Path) -> None:
    store = _seed(
        tmp_path,
        [
            _event(machine="a", source="/a/git", type="t1"),
            _event(machine="a", source="/a/mcp", type="t2"),
        ],
    )

    results = query_events(store.iter_events(), Query(source="/a/git"))

    assert [e.type for e in results] == ["t1"]


def test_filter_by_source_no_match_returns_empty(tmp_path: Path) -> None:
    store = _seed(tmp_path, [_event(machine="a", source="/a/git")])

    results = query_events(store.iter_events(), Query(source="/nope"))

    assert results == []


# --- filter by type (glob) ------------------------------------------------


def test_filter_by_type_exact(tmp_path: Path) -> None:
    store = _seed(
        tmp_path,
        [
            _event(machine="m", type="dev.x.mission.start"),
            _event(machine="m", type="dev.x.mission.end"),
        ],
    )

    results = query_events(store.iter_events(), Query(type="dev.x.mission.start"))

    assert [e.type for e in results] == ["dev.x.mission.start"]


def test_filter_by_type_glob_star_suffix(tmp_path: Path) -> None:
    store = _seed(
        tmp_path,
        [
            _event(machine="m", type="dev.x.mission.start", id="1" * 32),
            _event(machine="m", type="dev.x.mission.end", id="2" * 32),
            _event(machine="m", type="dev.x.commit", id="3" * 32),
        ],
    )

    results = query_events(store.iter_events(), Query(type="dev.x.mission.*"))

    assert sorted(e.type for e in results) == [
        "dev.x.mission.end",
        "dev.x.mission.start",
    ]


def test_filter_by_type_glob_question_mark(tmp_path: Path) -> None:
    store = _seed(
        tmp_path,
        [
            _event(machine="m", type="a.b", id="1" * 32),
            _event(machine="m", type="a.c", id="2" * 32),
            _event(machine="m", type="a.bc", id="3" * 32),
        ],
    )

    results = query_events(store.iter_events(), Query(type="a.?"))

    assert sorted(e.type for e in results) == ["a.b", "a.c"]


def test_filter_by_type_glob_matches_whole_string_not_substring(tmp_path: Path) -> None:
    # fnmatch is anchored — "mission" must NOT match "dev.x.mission.start".
    store = _seed(tmp_path, [_event(machine="m", type="dev.x.mission.start")])

    results = query_events(store.iter_events(), Query(type="mission"))

    assert results == []


def test_filter_by_type_glob_char_class(tmp_path: Path) -> None:
    store = _seed(
        tmp_path,
        [
            _event(machine="m", type="log.v1", id="1" * 32),
            _event(machine="m", type="log.v2", id="2" * 32),
            _event(machine="m", type="log.v9", id="3" * 32),
            _event(machine="m", type="log.vx", id="4" * 32),
        ],
    )

    results = query_events(store.iter_events(), Query(type="log.v[12]"))

    assert sorted(e.type for e in results) == ["log.v1", "log.v2"]


# --- filter by machine ----------------------------------------------------


def test_filter_by_machine(tmp_path: Path) -> None:
    store = _seed(
        tmp_path,
        [
            _event(machine="laptop", type="t1"),
            _event(machine="server", type="t2"),
        ],
    )

    results = query_events(store.iter_events(), Query(machine="laptop"))

    assert [e.type for e in results] == ["t1"]


# --- time range (since / until) -------------------------------------------


def test_filter_since_is_inclusive_lower_bound(tmp_path: Path) -> None:
    store = _seed(
        tmp_path,
        [
            _event(machine="m", type="early", time="2026-01-01T00:00:00Z", id="1" * 32),
            _event(machine="m", type="boundary", time="2026-06-01T00:00:00Z", id="2" * 32),
            _event(machine="m", type="late", time="2026-12-01T00:00:00Z", id="3" * 32),
        ],
    )

    results = query_events(
        store.iter_events(), Query(since="2026-06-01T00:00:00Z")
    )

    assert sorted(e.type for e in results) == ["boundary", "late"]


def test_filter_until_is_exclusive_upper_bound(tmp_path: Path) -> None:
    store = _seed(
        tmp_path,
        [
            _event(machine="m", type="early", time="2026-01-01T00:00:00Z", id="1" * 32),
            _event(machine="m", type="boundary", time="2026-06-01T00:00:00Z", id="2" * 32),
            _event(machine="m", type="late", time="2026-12-01T00:00:00Z", id="3" * 32),
        ],
    )

    results = query_events(
        store.iter_events(), Query(until="2026-06-01T00:00:00Z")
    )

    assert [e.type for e in results] == ["early"]


def test_filter_since_and_until_form_a_half_open_window(tmp_path: Path) -> None:
    store = _seed(
        tmp_path,
        [
            _event(machine="m", type="a", time="2026-01-01T00:00:00Z", id="1" * 32),
            _event(machine="m", type="b", time="2026-06-15T00:00:00Z", id="2" * 32),
            _event(machine="m", type="c", time="2026-12-01T00:00:00Z", id="3" * 32),
        ],
    )

    results = query_events(
        store.iter_events(),
        Query(since="2026-06-01T00:00:00Z", until="2026-07-01T00:00:00Z"),
    )

    assert [e.type for e in results] == ["b"]


def test_time_filter_accepts_datetime_objects(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    store = _seed(
        tmp_path,
        [
            _event(machine="m", type="early", time="2026-01-01T00:00:00Z", id="1" * 32),
            _event(machine="m", type="late", time="2026-12-01T00:00:00Z", id="2" * 32),
        ],
    )

    results = query_events(
        store.iter_events(),
        Query(since=datetime(2026, 6, 1, tzinfo=timezone.utc)),
    )

    assert [e.type for e in results] == ["late"]


def test_time_filter_compares_across_offsets(tmp_path: Path) -> None:
    # An event written with a non-Z offset compares correctly against a Z bound.
    events = [
        # 2026-06-01T02:00:00+02:00 == 2026-06-01T00:00:00Z
        _event(machine="m", type="boundary", time="2026-06-01T02:00:00+02:00", seq=0),
    ]

    included = query_events(events, Query(since="2026-06-01T00:00:00Z"))
    excluded = query_events(events, Query(until="2026-06-01T00:00:00Z"))

    assert [e.type for e in included] == ["boundary"]
    assert excluded == []


# --- combined filters are ANDed -------------------------------------------


def test_filters_are_anded_together(tmp_path: Path) -> None:
    store = _seed(
        tmp_path,
        [
            _event(machine="laptop", source="/laptop/git", type="git.commit",
                   time="2026-06-10T00:00:00Z", id="1" * 32),
            _event(machine="laptop", source="/laptop/mcp", type="git.commit",
                   time="2026-06-10T00:00:00Z", id="2" * 32),
            _event(machine="server", source="/server/git", type="git.commit",
                   time="2026-06-10T00:00:00Z", id="3" * 32),
            _event(machine="laptop", source="/laptop/git", type="git.push",
                   time="2026-06-10T00:00:00Z", id="4" * 32),
            _event(machine="laptop", source="/laptop/git", type="git.commit",
                   time="2026-01-01T00:00:00Z", id="5" * 32),
        ],
    )

    results = query_events(
        store.iter_events(),
        Query(
            machine="laptop",
            source="/laptop/git",
            type="git.*",
            since="2026-06-01T00:00:00Z",
        ),
    )

    # Only events 1 and 4 satisfy machine AND source AND type-glob AND since.
    assert sorted(e.id for e in results) == ["1" * 32, "4" * 32]


# --- ordering by (time, seq) ----------------------------------------------


def test_results_ordered_by_time_then_seq(tmp_path: Path) -> None:
    # Feed events out of time order; query must sort by time ascending.
    events = [
        _event(machine="m", type="c", time="2026-03-01T00:00:00Z", seq=2),
        _event(machine="m", type="a", time="2026-01-01T00:00:00Z", seq=0),
        _event(machine="m", type="b", time="2026-02-01T00:00:00Z", seq=1),
    ]

    results = query_events(events, Query())

    assert [e.type for e in results] == ["a", "b", "c"]


def test_same_time_tie_broken_by_seq(tmp_path: Path) -> None:
    events = [
        _event(machine="m", type="third", time="2026-01-01T00:00:00Z", seq=5),
        _event(machine="m", type="first", time="2026-01-01T00:00:00Z", seq=1),
        _event(machine="m", type="second", time="2026-01-01T00:00:00Z", seq=3),
    ]

    results = query_events(events, Query())

    assert [e.type for e in results] == ["first", "second", "third"]


def test_ordering_sorts_by_instant_not_lexically(tmp_path: Path) -> None:
    # Different textual offsets for the same instant ordering must use parsed time.
    events = [
        # 2026-01-01T00:00:00Z
        _event(machine="m", type="utc", time="2026-01-01T00:00:00Z", seq=0),
        # 2025-12-31T23:00:00-02:00 == 2026-01-01T01:00:00Z (later instant,
        # but lexically "2025..." sorts earlier)
        _event(machine="m", type="offset", time="2025-12-31T23:00:00-02:00", seq=1),
    ]

    results = query_events(events, Query())

    assert [e.type for e in results] == ["utc", "offset"]


def test_none_seq_sorts_before_assigned_seq_at_same_time(tmp_path: Path) -> None:
    # A freeform/unstored event (seq=None) should not crash the sort and is
    # treated as preceding any assigned seq at the same instant.
    events = [
        _event(machine="m", type="stored", time="2026-01-01T00:00:00Z", seq=0),
        _event(machine="m", type="unstored", time="2026-01-01T00:00:00Z", seq=None),
    ]

    results = query_events(events, Query())

    assert [e.type for e in results] == ["unstored", "stored"]


# --- purity / no persistence ----------------------------------------------


def test_query_does_not_consume_or_mutate_the_source_events(tmp_path: Path) -> None:
    events = [
        _event(machine="m", type="a", time="2026-01-01T00:00:00Z", seq=0),
        _event(machine="m", type="b", time="2026-02-01T00:00:00Z", seq=1),
    ]
    before = list(events)

    query_events(events, Query(type="a"))

    # Source list untouched; events are frozen dataclasses (immutable).
    assert events == before


def test_query_writes_nothing_to_disk(tmp_path: Path) -> None:
    store = _seed(tmp_path, [_event(machine="m", type="a")])
    before = sorted(p.name for p in (tmp_path / "m").iterdir())

    query_events(store.iter_events(), Query(type="a"))

    after = sorted(p.name for p in (tmp_path / "m").iterdir())
    assert after == before


# --- Query value object ---------------------------------------------------


def test_query_is_frozen(tmp_path: Path) -> None:
    q = Query(type="t")
    with pytest.raises(FrozenInstanceError):
        q.type = "other"  # type: ignore[misc]


def test_query_method_on_store_iterable_round_trips(tmp_path: Path) -> None:
    # Convenience: filtering a stored ledger end to end.
    store = _seed(
        tmp_path,
        [
            _event(machine="m", type="keep", time="2026-06-01T00:00:00Z", id="1" * 32),
            _event(machine="m", type="drop", time="2026-01-01T00:00:00Z", id="2" * 32),
        ],
    )

    results = query_events(
        store.iter_events(), Query(type="keep", since="2026-05-01T00:00:00Z")
    )

    assert len(results) == 1
    assert results[0].type == "keep"
