"""evledger quickstart — append a few events, then query and aggregate them.

Run it directly (no setup, writes to a throwaway temp dir):

    python examples/quickstart.py

It exercises the pure-stdlib core: the store (append + read), the query layer
(filter by type/time), and the stats layer (counts + paired *.start/*.end
durations). No CLI, MCP, or third-party dependencies involved.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from evledger import (
    LedgerStore,
    Query,
    new_event,
    query_events,
)
from evledger.stats import counts_by_type, pair_events


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "ledger"
        store = LedgerStore(root=root)

        # 1. Append events. `seq` is assigned per-machine, monotonic.
        store.append(new_event(
            source="/laptop/app", type="com.example.job.start",
            machine="laptop", time="2026-05-30T10:00:00Z",
            data={"job": "nightly-sync"},
        ))
        store.append(new_event(
            source="/laptop/app", type="com.example.job.end",
            machine="laptop", time="2026-05-30T10:04:30Z",
            data={"job": "nightly-sync", "ok": True},
        ))
        store.append(new_event(
            source="/laptop/app", type="com.example.job.start",
            machine="laptop", time="2026-05-30T11:00:00Z",
            data={"job": "report"},
        ))

        # 2. Read everything back (ordered by time, then seq).
        events = store.read_all().events
        print(f"stored {len(events)} events in {root}")

        # 3. Query — only the 'start' events.
        starts = query_events(events, Query(type="com.example.job.start"))
        print(f"start events: {len(starts)}")

        # 4. Stats — counts by type.
        print("counts by type:")
        for event_type, n in sorted(counts_by_type(events).items()):
            print(f"  {n:>2}  {event_type}")

        # 5. Pair *.start/*.end into durations.
        pairing = pair_events(events)  # defaults: pair *.start with *.end
        for d in pairing.durations:
            print(f"duration: {d.seconds:.0f}s for {d.base}")
        if pairing.unmatched_starts:
            print(f"unmatched starts: {len(pairing.unmatched_starts)} (the 'report' job never ended)")


if __name__ == "__main__":
    main()
