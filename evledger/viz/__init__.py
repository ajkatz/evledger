"""Backend-agnostic derivation layer for the ledger visualizer.

This package turns a stream of :class:`~evledger.LedgerEvent` into the
shapes a visualizer needs — nested **spans** (flame-graph rows) and **link**
edges (the "connected items" overlay) — using only inferred connectivity (no
schema change). It is *pure* (no I/O, no persistence) and imports only the
public :mod:`evledger` surface plus the standard library, so the
``serve`` command can call it without web/MCP dependencies.

Public symbols (the ``viz-connections`` task)::

    from evledger.viz import derive_connections

    conn = derive_connections(events)
    conn.spans   # tuple[Span, ...]  — nested by time-containment
    conn.links   # tuple[Link, ...]  — start/end pairs + shared-data edges

:class:`Span`
    A derived ``*.start`` / ``*.end`` interval with ``depth`` / ``parent``.
:class:`Link`
    A directed ``{from_event_id, to_event_id, kind}`` edge.
:class:`Connections`
    The combined ``spans`` + ``links`` result.
:func:`derive_spans`, :func:`derive_links`, :func:`derive_connections`
    The pure derivation entry points.
"""

from __future__ import annotations

from evledger.viz.connections import (
    Connections,
    Link,
    Span,
    derive_connections,
    derive_links,
    derive_spans,
)

__all__ = [
    "Span",
    "Link",
    "Connections",
    "derive_spans",
    "derive_links",
    "derive_connections",
]
