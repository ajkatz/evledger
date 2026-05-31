"""Unit tests for :mod:`evledger.store` — the append-only JSONL store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evledger import (
    LedgerEvent,
    LedgerStore,
    ReadResult,
    new_event,
)


def _event(machine: str = "m", type: str = "t", **kw: object) -> LedgerEvent:
    """Build an event with a deterministic id/time unless overridden."""
    kw.setdefault("source", f"/{machine}/sys")
    kw.setdefault("id", "0" * 32)
    kw.setdefault("time", "2026-05-29T14:30:15Z")
    return new_event(machine=machine, type=type, **kw)  # type: ignore[arg-type]


# --- partition path layout ------------------------------------------------


def test_append_writes_to_machine_partitioned_time_bucketed_file(tmp_path: Path) -> None:
    store = LedgerStore(root=tmp_path)
    store.append(_event(machine="laptop", time="2026-05-29T14:30:15Z"))

    expected = tmp_path / "laptop" / "2026-05.jsonl"
    assert expected.exists()


def test_append_time_bucket_uses_event_time_month(tmp_path: Path) -> None:
    store = LedgerStore(root=tmp_path)
    store.append(_event(machine="laptop", time="2026-01-02T03:04:05Z"))
    store.append(_event(machine="laptop", time="2026-12-31T23:59:59Z"))

    assert (tmp_path / "laptop" / "2026-01.jsonl").exists()
    assert (tmp_path / "laptop" / "2026-12.jsonl").exists()


def test_append_separates_partitions_by_machine(tmp_path: Path) -> None:
    store = LedgerStore(root=tmp_path)
    store.append(_event(machine="laptop"))
    store.append(_event(machine="server"))

    assert (tmp_path / "laptop" / "2026-05.jsonl").exists()
    assert (tmp_path / "server" / "2026-05.jsonl").exists()


# --- one CloudEvent per line ----------------------------------------------


def test_append_writes_one_valid_json_line_terminated_by_newline(tmp_path: Path) -> None:
    store = LedgerStore(root=tmp_path)
    store.append(_event(machine="m", data={"k": "v"}))

    text = (tmp_path / "m" / "2026-05.jsonl").read_text(encoding="utf-8")
    assert text.endswith("\n")
    lines = text.splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["data"] == {"k": "v"}


def test_append_returns_the_stored_event_with_seq(tmp_path: Path) -> None:
    store = LedgerStore(root=tmp_path)
    stored = store.append(_event(machine="m"))

    assert stored.seq == 0


def test_append_preserves_non_ascii(tmp_path: Path) -> None:
    store = LedgerStore(root=tmp_path)
    store.append(_event(machine="m", data={"name": "café"}))

    text = (tmp_path / "m" / "2026-05.jsonl").read_text(encoding="utf-8")
    assert "café" in text


# --- monotonic per-machine seq --------------------------------------------


def test_append_assigns_monotonic_seq_starting_at_zero(tmp_path: Path) -> None:
    store = LedgerStore(root=tmp_path)
    a = store.append(_event(machine="m"))
    b = store.append(_event(machine="m"))
    c = store.append(_event(machine="m"))

    assert [a.seq, b.seq, c.seq] == [0, 1, 2]


def test_seq_is_continuous_across_month_buckets(tmp_path: Path) -> None:
    store = LedgerStore(root=tmp_path)
    a = store.append(_event(machine="m", time="2026-01-15T00:00:00Z"))
    b = store.append(_event(machine="m", time="2026-02-15T00:00:00Z"))

    assert a.seq == 0
    assert b.seq == 1


def test_seq_is_independent_per_machine(tmp_path: Path) -> None:
    store = LedgerStore(root=tmp_path)
    m0 = store.append(_event(machine="m"))
    n0 = store.append(_event(machine="n"))
    m1 = store.append(_event(machine="m"))

    assert m0.seq == 0
    assert n0.seq == 0
    assert m1.seq == 1


def test_seq_resumes_from_existing_files_on_new_store(tmp_path: Path) -> None:
    # First store instance writes two events.
    store1 = LedgerStore(root=tmp_path)
    store1.append(_event(machine="m"))
    store1.append(_event(machine="m"))

    # A fresh store (e.g. a new process) must continue the sequence.
    store2 = LedgerStore(root=tmp_path)
    third = store2.append(_event(machine="m"))

    assert third.seq == 2


def test_append_overrides_any_preexisting_seq_on_the_event(tmp_path: Path) -> None:
    store = LedgerStore(root=tmp_path)
    # Even if the caller passes an event that already carries a seq, the store
    # is authoritative and assigns the next monotonic value.
    preset = _event(machine="m").with_seq(999)
    stored = store.append(preset)

    assert stored.seq == 0


# --- append-only invariant ------------------------------------------------


def test_append_never_rewrites_existing_lines(tmp_path: Path) -> None:
    store = LedgerStore(root=tmp_path)
    store.append(_event(machine="m", id="a" * 32))
    first_line = (tmp_path / "m" / "2026-05.jsonl").read_text(encoding="utf-8").splitlines()[0]

    store.append(_event(machine="m", id="b" * 32))
    lines = (tmp_path / "m" / "2026-05.jsonl").read_text(encoding="utf-8").splitlines()

    assert len(lines) == 2
    assert lines[0] == first_line  # original line byte-for-byte unchanged


# --- iter_events / read_all -----------------------------------------------


def test_read_all_returns_events_from_all_partitions(tmp_path: Path) -> None:
    store = LedgerStore(root=tmp_path)
    store.append(_event(machine="laptop", type="a"))
    store.append(_event(machine="server", type="b"))

    result = store.read_all()

    types = sorted(e.type for e in result.events)
    assert types == ["a", "b"]
    assert result.malformed == 0


def test_read_all_on_missing_root_returns_empty(tmp_path: Path) -> None:
    store = LedgerStore(root=tmp_path / "does-not-exist")

    result = store.read_all()

    assert result.events == []
    assert result.malformed == 0


def test_read_all_orders_within_a_partition_by_file_order(tmp_path: Path) -> None:
    store = LedgerStore(root=tmp_path)
    store.append(_event(machine="m", id="1" * 32))
    store.append(_event(machine="m", id="2" * 32))

    result = store.read_all()
    seqs = [e.seq for e in result.events]

    assert seqs == [0, 1]


def test_iter_events_yields_lazily(tmp_path: Path) -> None:
    store = LedgerStore(root=tmp_path)
    store.append(_event(machine="m"))
    store.append(_event(machine="m"))

    events = list(store.iter_events())

    assert len(events) == 2
    assert all(isinstance(e, LedgerEvent) for e in events)


# --- malformed-line tolerance (drop + count) ------------------------------


def test_read_all_drops_and_counts_malformed_lines(tmp_path: Path) -> None:
    part = tmp_path / "m"
    part.mkdir(parents=True)
    good = new_event(source="/m/s", type="t", machine="m", id="a" * 32).with_seq(0)
    (part / "2026-05.jsonl").write_text(
        good.to_jsonl_line() + "\n"
        + "this is not json\n"
        + '{"missing":"required fields"}\n',
        encoding="utf-8",
    )

    store = LedgerStore(root=tmp_path)
    result = store.read_all()

    assert len(result.events) == 1
    assert result.events[0].id == "a" * 32
    assert result.malformed == 2


def test_read_all_skips_blank_lines_without_counting_them(tmp_path: Path) -> None:
    part = tmp_path / "m"
    part.mkdir(parents=True)
    good = new_event(source="/m/s", type="t", machine="m", id="a" * 32).with_seq(0)
    (part / "2026-05.jsonl").write_text(
        "\n" + good.to_jsonl_line() + "\n" + "   \n",
        encoding="utf-8",
    )

    store = LedgerStore(root=tmp_path)
    result = store.read_all()

    assert len(result.events) == 1
    assert result.malformed == 0


def test_read_all_ignores_non_jsonl_files(tmp_path: Path) -> None:
    part = tmp_path / "m"
    part.mkdir(parents=True)
    (part / "README.txt").write_text("not a ledger file", encoding="utf-8")
    store = LedgerStore(root=tmp_path)
    store.append(_event(machine="m"))

    result = store.read_all()

    assert len(result.events) == 1
    assert result.malformed == 0


def test_lockfile_is_not_parsed_as_an_event(tmp_path: Path) -> None:
    store = LedgerStore(root=tmp_path)
    store.append(_event(machine="m"))

    result = store.read_all()

    # The lock file (whatever its name) must not appear as a malformed event.
    assert len(result.events) == 1
    assert result.malformed == 0


# --- concurrent same-machine appends --------------------------------------


def test_concurrent_appends_do_not_corrupt_or_lose_lines(tmp_path: Path) -> None:
    import threading

    store = LedgerStore(root=tmp_path)
    n_threads = 8
    per_thread = 25
    barrier = threading.Barrier(n_threads)

    def worker(tid: int) -> None:
        barrier.wait()
        for i in range(per_thread):
            store.append(_event(machine="m", type=f"t{tid}-{i}", id=f"{tid:02d}{i:030d}"))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    result = store.read_all()
    # Every append landed, no lines lost or corrupted.
    assert len(result.events) == n_threads * per_thread
    assert result.malformed == 0
    # seq values are a contiguous 0..N-1 set (monotonic, no duplicates/gaps).
    seqs = sorted(e.seq for e in result.events)
    assert seqs == list(range(n_threads * per_thread))


# --- generic config / no claude coupling ----------------------------------


def test_store_root_is_a_required_explicit_argument(tmp_path: Path) -> None:
    # The store must not hardcode any root; it takes one explicitly.
    store = LedgerStore(root=tmp_path / "custom" / "ledger")
    store.append(_event(machine="m"))

    assert (tmp_path / "custom" / "ledger" / "m" / "2026-05.jsonl").exists()
