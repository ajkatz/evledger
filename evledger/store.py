"""Append-only, atomic, multi-writer-safe JSONL store for the ledger.

This module is the **write + read path** of the ledger (the ``ledger-store``
task of the ``universal-event-ledger`` whim). It builds only on the schema
layer (:class:`~evledger.schema.LedgerEvent` and its helpers) and the
standard library — it imports **nothing** from sibling ``claude_kg`` modules,
so the whole ledger stays extractable to its own package (Decision 9). In
particular it does *not* reuse ``concurrency.py``; the atomic-append +
lockfile mechanism is re-derived here from stdlib primitives.

Storage layout (Decision 2 — machine-partitioned, time-bucketed)::

    <root>/<machine-id>/<YYYY-MM>.jsonl

A machine only ever appends to its own subtree, so concurrent *machines*
never conflict on sync (federation is git clone/pull/push). Concurrent
*agents on one machine* are serialized by a per-machine lock file
(:data:`LOCK_FILENAME`) held across the read-seq + append critical section.

Invariants honored here:

* **Append-only.** Existing lines are never rewritten, reordered, or
  compacted. Each :meth:`LedgerStore.append` adds exactly one line.
* **Never compress.** Plain UTF-8 JSONL, one CloudEvent per line.
* **Monotonic per-machine ``seq``.** The schema leaves ``seq`` as ``None``;
  the store assigns the next per-machine value under the lock so concurrent
  appends get distinct, gap-free sequence numbers.
* **Read tolerance.** :meth:`LedgerStore.read_all` / :meth:`iter_events`
  tolerate a missing root, ignore non-``.jsonl`` files and the lock file, skip
  blank lines, and *drop + count* malformed lines rather than failing.

The ``root`` is always an explicit constructor argument with no hardcoded
default path, so an adopter points the store at their own ledger directory.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from evledger.schema import LedgerEvent, parse_time

try:  # POSIX advisory locking via flock.
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]
    _HAVE_FCNTL = False

if not _HAVE_FCNTL:  # pragma: no cover - Windows fallback
    import msvcrt

#: Name of the per-machine lock file written inside ``<root>/<machine-id>/``.
#: It is not a ``.jsonl`` file, so the reader naturally ignores it.
LOCK_FILENAME = ".ledger.lock"

#: Suffix identifying ledger data files within a partition directory.
JSONL_SUFFIX = ".jsonl"


@dataclass(frozen=True)
class ReadResult:
    """The outcome of reading the ledger.

    Attributes:
        events: The successfully parsed events, in file/partition iteration
            order (per-machine partitions in sorted machine-id order; month
            buckets in sorted name order; lines in append order within a file).
        malformed: The number of non-blank lines that could not be parsed into
            a valid event and were dropped.
    """

    events: list[LedgerEvent] = field(default_factory=list)
    malformed: int = 0


def _month_bucket(event_time: str) -> str:
    """Return the ``YYYY-MM`` time-bucket name for an event's ``time``."""
    return parse_time(event_time).strftime("%Y-%m")


class LedgerStore:
    """An append-only JSONL ledger rooted at a directory.

    Args:
        root: The ledger root directory (the ``ledger/`` *instance*). Created
            on first append. No default — the caller supplies it (e.g. from a
            ``--ledger-root`` flag or ``$CLAUDE_LEDGER_ROOT``), so the store
            stays free of any hardcoded path.

    A single :class:`LedgerStore` is safe to share across threads, and
    multiple :class:`LedgerStore` instances / OS processes pointed at the same
    ``root`` coordinate through the on-disk per-machine lock file.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """The ledger root directory."""
        return self._root

    # -- write path --------------------------------------------------------

    def append(self, event: LedgerEvent) -> LedgerEvent:
        """Append one event to its machine partition and return the stored copy.

        The event's ``machine`` selects the partition; its ``time`` selects the
        ``YYYY-MM`` bucket. The store assigns the next monotonic per-machine
        ``seq`` (overriding any ``seq`` already on the passed event) under an
        exclusive per-machine lock, then writes a single newline-terminated
        JSONL line with an atomic ``O_APPEND`` write and ``fsync``.

        Returns:
            A copy of ``event`` with the assigned ``seq`` — the exact object
            that was persisted.
        """
        partition_dir = self._root / event.machine
        partition_dir.mkdir(parents=True, exist_ok=True)

        with _PartitionLock(partition_dir / LOCK_FILENAME):
            seq = self._next_seq(partition_dir)
            stored = event.with_seq(seq)
            target = partition_dir / f"{_month_bucket(stored.time)}{JSONL_SUFFIX}"
            _atomic_append_line(target, stored.to_jsonl_line())
        return stored

    def _next_seq(self, partition_dir: Path) -> int:
        """Compute the next per-machine ``seq`` by scanning the partition.

        Called while holding the partition lock. The next ``seq`` is one past
        the highest ``seq`` already recorded in any of the machine's month
        buckets; an empty/new partition starts at ``0``. Lines without a
        parseable ``seq`` are ignored so a hand-edited or malformed file can't
        stall the sequence.
        """
        highest = -1
        for path in self._partition_files(partition_dir):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = LedgerEvent.from_jsonl_line(stripped)
                except (ValueError, TypeError):
                    continue
                if event.seq is not None and event.seq > highest:
                    highest = event.seq
        return highest + 1

    @staticmethod
    def _partition_files(partition_dir: Path) -> list[Path]:
        """Return the ``.jsonl`` files in a partition, in sorted name order."""
        if not partition_dir.is_dir():
            return []
        return sorted(
            p for p in partition_dir.iterdir()
            if p.is_file() and p.suffix == JSONL_SUFFIX
        )

    # -- read path ---------------------------------------------------------

    def iter_events(self) -> Iterator[LedgerEvent]:
        """Yield every parseable event across all machine partitions, lazily.

        Tolerant of a missing root and of malformed/blank lines (silently
        skipped here — use :meth:`read_all` to also get the dropped-line
        count). Iteration order is deterministic: machine partitions in sorted
        machine-id order, month buckets in sorted name order, lines in append
        order within a file.
        """
        for event, _malformed in self._scan():
            if event is not None:
                yield event

    def read_all(self) -> ReadResult:
        """Read every event across all partitions, counting dropped lines.

        Returns:
            A :class:`ReadResult` with the parsed ``events`` and the count of
            ``malformed`` (non-blank, unparseable) lines that were dropped.
        """
        events: list[LedgerEvent] = []
        malformed = 0
        for event, dropped in self._scan():
            if event is not None:
                events.append(event)
            malformed += dropped
        return ReadResult(events=events, malformed=malformed)

    def _scan(self) -> Iterator[tuple[LedgerEvent | None, int]]:
        """Walk all partitions, yielding ``(event_or_None, malformed_delta)``.

        Blank/whitespace-only lines yield ``(None, 0)`` (skipped, not counted);
        unparseable non-blank lines yield ``(None, 1)``; good lines yield
        ``(event, 0)``.
        """
        if not self._root.is_dir():
            return
        for machine_dir in sorted(p for p in self._root.iterdir() if p.is_dir()):
            for path in self._partition_files(machine_dir):
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                for line in text.splitlines():
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        yield LedgerEvent.from_jsonl_line(stripped), 0
                    except (ValueError, TypeError):
                        yield None, 1


class _PartitionLock:
    """An exclusive, advisory file lock over a partition's lock file.

    Used as a context manager. On POSIX it uses ``fcntl.flock`` (exclusive);
    on Windows it falls back to ``msvcrt.locking``. The lock file itself is
    never a ``.jsonl`` file, so the reader ignores it. The lock serializes the
    read-seq + append critical section so concurrent same-machine writers get
    distinct ``seq`` values and never interleave partial lines.
    """

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._fd: int | None = None

    def __enter__(self) -> _PartitionLock:
        self._fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        if _HAVE_FCNTL:
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        else:  # pragma: no cover - Windows fallback
            msvcrt.locking(self._fd, msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            try:
                if _HAVE_FCNTL:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                else:  # pragma: no cover - Windows fallback
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            finally:
                os.close(self._fd)
                self._fd = None


def _atomic_append_line(target: Path, line: str) -> None:
    """Append a single newline-terminated line to ``target`` atomically.

    Opens with ``O_APPEND`` so the kernel positions every write at the current
    end of file (POSIX guarantees an ``O_APPEND`` ``write`` is atomic against
    other ``O_APPEND`` writers), then issues the payload as a single
    ``os.write`` and ``fsync``s so the line is durable. Combined with the
    partition lock held by the caller, concurrent same-machine appends never
    interleave or corrupt lines.
    """
    payload = (line + "\n").encode("utf-8")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
