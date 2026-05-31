"""Unit tests for :mod:`evledger.viz.connections`.

The connections derivation is *pure*: it takes any iterable of
:class:`~evledger.LedgerEvent`, reuses ``stats.pair_events`` to build
spans, nests them by time-containment, and computes link edges. It writes
nothing and never mutates the frozen events. Frozen value objects expose
*tuple* fields (compare to ``()`` not ``[]``).
"""

from __future__ import annotations

from evledger import LedgerEvent, new_event
from evledger.viz import (
    Link,
    Span,
    derive_connections,
    derive_links,
    derive_spans,
)


def _event(
    *,
    type: str,
    time: str,
    id: str,
    machine: str = "m",
    source: str | None = None,
    data: object | None = None,
    seq: int | None = None,
) -> LedgerEvent:
    """Build a fully-formed event with explicit id/time for determinism."""
    ev = new_event(
        machine=machine,
        type=type,
        source=source if source is not None else f"/{machine}/sys",
        data=data,
        id=id,
        time=time,
    )
    return ev if seq is None else ev.with_seq(seq)


# --- span derivation ------------------------------------------------------


def test_single_pair_becomes_one_span() -> None:
    events = [
        _event(type="dev.x.mission.start", time="2026-05-30T00:00:00Z", id="a"),
        _event(type="dev.x.mission.end", time="2026-05-30T00:01:00Z", id="b"),
    ]

    spans = derive_spans(events)

    assert len(spans) == 1
    span = spans[0]
    assert span.base == "dev.x.mission"
    assert span.depth == 0
    assert span.parent is None
    assert span.event_ids == ("a", "b")
    assert span.seconds == 60.0


def test_spans_field_is_a_tuple() -> None:
    spans = derive_spans([])
    assert spans == ()


def test_unpaired_start_produces_no_span() -> None:
    events = [
        _event(type="dev.x.mission.start", time="2026-05-30T00:00:00Z", id="a"),
    ]

    assert derive_spans(events) == ()


def test_unpaired_end_produces_no_span() -> None:
    events = [
        _event(type="dev.x.mission.end", time="2026-05-30T00:00:00Z", id="z"),
    ]

    assert derive_spans(events) == ()


def test_unpaired_start_and_end_mixed_with_a_real_pair() -> None:
    # One clean pair, plus a dangling start and a dangling end (different bases
    # so they don't get matched to each other).
    events = [
        _event(type="dev.x.a.start", time="2026-05-30T00:00:00Z", id="s1"),
        _event(type="dev.x.a.end", time="2026-05-30T00:02:00Z", id="e1"),
        _event(type="dev.x.b.start", time="2026-05-30T00:03:00Z", id="s2"),
        _event(type="dev.x.c.end", time="2026-05-30T00:04:00Z", id="e2"),
    ]

    spans = derive_spans(events)

    assert len(spans) == 1
    assert spans[0].base == "dev.x.a"
    assert spans[0].event_ids == ("s1", "e1")


# --- nesting --------------------------------------------------------------


def test_two_level_nesting_inner_under_outer() -> None:
    events = [
        _event(type="dev.x.outer.start", time="2026-05-30T00:00:00Z", id="o1"),
        _event(type="dev.x.inner.start", time="2026-05-30T00:00:10Z", id="i1"),
        _event(type="dev.x.inner.end", time="2026-05-30T00:00:20Z", id="i2"),
        _event(type="dev.x.outer.end", time="2026-05-30T00:00:30Z", id="o2"),
    ]

    spans = derive_spans(events)
    by_base = {s.base: s for s in spans}

    outer = by_base["dev.x.outer"]
    inner = by_base["dev.x.inner"]
    assert outer.depth == 0
    assert outer.parent is None
    assert inner.depth == 1
    assert inner.parent == outer.id


def test_three_level_nesting_assigns_increasing_depth() -> None:
    events = [
        _event(type="dev.x.l0.start", time="2026-05-30T00:00:00Z", id="a0"),
        _event(type="dev.x.l1.start", time="2026-05-30T00:00:05Z", id="b0"),
        _event(type="dev.x.l2.start", time="2026-05-30T00:00:10Z", id="c0"),
        _event(type="dev.x.l2.end", time="2026-05-30T00:00:15Z", id="c1"),
        _event(type="dev.x.l1.end", time="2026-05-30T00:00:20Z", id="b1"),
        _event(type="dev.x.l0.end", time="2026-05-30T00:00:25Z", id="a1"),
    ]

    spans = derive_spans(events)
    by_base = {s.base: s for s in spans}

    l0, l1, l2 = by_base["dev.x.l0"], by_base["dev.x.l1"], by_base["dev.x.l2"]
    assert (l0.depth, l1.depth, l2.depth) == (0, 1, 2)
    assert l0.parent is None
    assert l1.parent == l0.id
    # innermost parents to the *smallest* containing span (l1), not l0.
    assert l2.parent == l1.id


def test_overlapping_spans_are_not_nested() -> None:
    # A starts, B starts before A ends, A ends before B ends: they overlap but
    # neither interval contains the other, so both are depth 0 with no parent.
    events = [
        _event(type="dev.x.a.start", time="2026-05-30T00:00:00Z", id="a0"),
        _event(type="dev.x.b.start", time="2026-05-30T00:00:10Z", id="b0"),
        _event(type="dev.x.a.end", time="2026-05-30T00:00:20Z", id="a1"),
        _event(type="dev.x.b.end", time="2026-05-30T00:00:30Z", id="b1"),
    ]

    spans = derive_spans(events)
    by_base = {s.base: s for s in spans}

    assert by_base["dev.x.a"].depth == 0
    assert by_base["dev.x.a"].parent is None
    assert by_base["dev.x.b"].depth == 0
    assert by_base["dev.x.b"].parent is None


def test_equal_interval_does_not_self_nest() -> None:
    # Two spans with the identical interval: containment is strict, so neither
    # nests under the other (no parent loop, both depth 0).
    events = [
        _event(type="dev.x.a.start", time="2026-05-30T00:00:00Z", id="a0"),
        _event(type="dev.x.b.start", time="2026-05-30T00:00:00Z", id="b0"),
        _event(type="dev.x.a.end", time="2026-05-30T00:00:10Z", id="a1"),
        _event(type="dev.x.b.end", time="2026-05-30T00:00:10Z", id="b1"),
    ]

    spans = derive_spans(events)
    for span in spans:
        assert span.depth == 0
        assert span.parent is None


# --- links ----------------------------------------------------------------


def test_pair_link_connects_start_and_end() -> None:
    events = [
        _event(type="dev.x.mission.start", time="2026-05-30T00:00:00Z", id="a"),
        _event(type="dev.x.mission.end", time="2026-05-30T00:01:00Z", id="b"),
    ]

    links = derive_links(events)

    pair_links = [link for link in links if link.kind == "pair"]
    assert len(pair_links) == 1
    assert pair_links[0].from_event_id == "a"
    assert pair_links[0].to_event_id == "b"


def test_links_field_is_a_tuple() -> None:
    assert derive_links([]) == ()


def test_shared_data_key_links_two_events() -> None:
    events = [
        _event(
            type="dev.x.task.note",
            time="2026-05-30T00:00:00Z",
            id="n1",
            data={"whim": "ledger-viz"},
        ),
        _event(
            type="dev.x.task.note",
            time="2026-05-30T00:05:00Z",
            id="n2",
            data={"whim": "ledger-viz"},
        ),
    ]

    links = derive_links(events)
    data_links = [link for link in links if link.kind == "data:whim"]

    assert len(data_links) == 1
    endpoints = {data_links[0].from_event_id, data_links[0].to_event_id}
    assert endpoints == {"n1", "n2"}


def test_different_data_values_do_not_link() -> None:
    events = [
        _event(
            type="dev.x.task.note",
            time="2026-05-30T00:00:00Z",
            id="n1",
            data={"whim": "alpha"},
        ),
        _event(
            type="dev.x.task.note",
            time="2026-05-30T00:05:00Z",
            id="n2",
            data={"whim": "beta"},
        ),
    ]

    links = derive_links(events)
    assert [link for link in links if link.kind.startswith("data:")] == []


def test_three_events_sharing_a_key_link_pairwise_within_value() -> None:
    events = [
        _event(type="t", time="2026-05-30T00:00:00Z", id="x", data={"task": "T1"}),
        _event(type="t", time="2026-05-30T00:01:00Z", id="y", data={"task": "T1"}),
        _event(type="t", time="2026-05-30T00:02:00Z", id="z", data={"task": "T1"}),
    ]

    links = derive_links(events)
    data_links = [link for link in links if link.kind == "data:task"]

    # Chain the value-group in event order: x-y, y-z (no x-z duplicate edge).
    assert len(data_links) == 2
    edges = {(link.from_event_id, link.to_event_id) for link in data_links}
    assert edges == {("x", "y"), ("y", "z")}


def test_non_dict_data_is_ignored_for_links() -> None:
    events = [
        _event(type="t", time="2026-05-30T00:00:00Z", id="x", data="just a string"),
        _event(type="t", time="2026-05-30T00:01:00Z", id="y", data=["a", "list"]),
        _event(type="t", time="2026-05-30T00:02:00Z", id="z", data=None),
    ]

    assert derive_links(events) == ()


def test_unhashable_data_value_is_skipped() -> None:
    # A dict-valued data field can't be a grouping value; it must not crash.
    events = [
        _event(type="t", time="2026-05-30T00:00:00Z", id="x", data={"k": {"nested": 1}}),
        _event(type="t", time="2026-05-30T00:01:00Z", id="y", data={"k": {"nested": 1}}),
    ]

    assert [link for link in derive_links(events) if link.kind.startswith("data:")] == []


# --- combined --------------------------------------------------------------


def test_derive_connections_returns_spans_and_links() -> None:
    events = [
        _event(
            type="dev.x.mission.start",
            time="2026-05-30T00:00:00Z",
            id="a",
            data={"whim": "viz"},
        ),
        _event(
            type="dev.x.mission.end",
            time="2026-05-30T00:01:00Z",
            id="b",
            data={"whim": "viz"},
        ),
    ]

    conn = derive_connections(events)

    assert isinstance(conn.spans, tuple)
    assert isinstance(conn.links, tuple)
    assert len(conn.spans) == 1
    # one pair link + one data:whim link
    kinds = sorted(link.kind for link in conn.links)
    assert kinds == ["data:whim", "pair"]


def test_value_objects_are_frozen() -> None:
    span = Span(
        id="s",
        base="dev.x.a",
        start="2026-05-30T00:00:00Z",
        end="2026-05-30T00:01:00Z",
        seconds=60.0,
        depth=0,
        parent=None,
        event_ids=("a", "b"),
    )
    link = Link(from_event_id="a", to_event_id="b", kind="pair")

    import dataclasses

    import pytest

    with pytest.raises(dataclasses.FrozenInstanceError):
        span.depth = 1  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        link.kind = "data:x"  # type: ignore[misc]


def test_started_done_suffix_pairs_into_a_span() -> None:
    # The taxonomy names task events .started/.done (not .start/.end). Span
    # derivation must recognize that convention too, else task spans never form.
    events = [
        _event(type="dev.claude.task.started", time="2026-05-30T10:05:00Z", id="a"),
        _event(type="dev.claude.task.done", time="2026-05-30T10:20:00Z", id="b"),
    ]
    spans = derive_spans(events)
    assert len(spans) == 1
    assert spans[0].base == "dev.claude.task"
    assert spans[0].event_ids == ("a", "b")


def test_mixed_start_end_and_started_done_both_pair_and_nest() -> None:
    # A .start/.end mission containing a .started/.done task: both conventions
    # pair in one pass, and the task nests under the mission by containment.
    events = [
        _event(type="dev.claude.mission.start", time="2026-05-30T10:00:00Z", id="m0"),
        _event(type="dev.claude.task.started", time="2026-05-30T10:05:00Z", id="t0"),
        _event(type="dev.claude.task.done", time="2026-05-30T10:20:00Z", id="t1"),
        _event(type="dev.claude.mission.end", time="2026-05-30T10:50:00Z", id="m1"),
    ]
    spans = {s.base: s for s in derive_spans(events)}
    assert set(spans) == {"dev.claude.mission", "dev.claude.task"}
    assert spans["dev.claude.mission"].depth == 0
    assert spans["dev.claude.task"].depth == 1
    assert spans["dev.claude.task"].parent == spans["dev.claude.mission"].id
