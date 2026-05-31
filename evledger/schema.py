"""CloudEvents 1.0-aligned event envelope for the ledger.

This module defines :class:`LedgerEvent` — a single, point-in-time event in
the ledger — together with the time formatting/parsing helpers and a
:func:`new_event` factory.

The envelope follows the `CloudEvents 1.0 specification
<https://cloudevents.io/>`_ so that the on-disk JSON is interoperable with
standard CloudEvents tooling. The required context attributes are:

* ``specversion`` — always ``"1.0"``.
* ``id`` — a uuid4 hex string, unique within a ``source``.
* ``source`` — a URI-reference identifying the producing context. The ledger
  convention is ``"/<machine-id>/<system>"`` but any URI-reference is valid;
  the schema layer does not impose a taxonomy.
* ``type`` — a reverse-DNS event name, e.g. ``"dev.example.mission.start"``.
* ``time`` — ISO-8601 UTC with a ``Z`` suffix (see :func:`format_time`).
* ``datacontenttype`` — defaults to ``"application/json"``.
* ``data`` — an optional freeform JSON payload.

Two CloudEvents *extension attributes* carry ledger-specific bookkeeping:

* ``machine`` — the partition key; the machine that produced the event.
* ``seq`` — a per-machine monotonic tiebreaker, assigned by the store at
  append time (``None`` until then).

Events are *instants*: there is deliberately no ``duration`` attribute.
Durations are derived downstream by pairing ``*.start`` / ``*.end`` events.

This module is standalone (stdlib-only) and imports nothing from sibling
``claude_kg`` modules, so the ledger can be extracted to its own package
without a rewrite.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

#: The CloudEvents spec version this envelope targets.
SPEC_VERSION = "1.0"

#: Default media type for the ``data`` payload.
DEFAULT_CONTENT_TYPE = "application/json"

#: Context attributes that every well-formed event must carry.
_REQUIRED_FIELDS = ("specversion", "id", "source", "type", "time", "machine")


def now_utc() -> datetime:
    """Return the current time as a timezone-aware UTC :class:`datetime`."""
    return datetime.now(timezone.utc)


def format_time(dt: datetime) -> str:
    """Format an aware datetime as ISO-8601 UTC with a ``Z`` suffix.

    The value is converted to UTC first. Sub-second precision is preserved
    only when present (microseconds are dropped from the rendering when zero).

    Raises:
        ValueError: if ``dt`` is naive (has no timezone).
    """
    if dt.tzinfo is None:
        raise ValueError("format_time requires a timezone-aware datetime")
    utc = dt.astimezone(timezone.utc)
    # isoformat() renders "+00:00"; normalize to the canonical "Z" suffix.
    text = utc.isoformat()
    if text.endswith("+00:00"):
        text = text[: -len("+00:00")] + "Z"
    return text


def parse_time(text: str) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC :class:`datetime`.

    Accepts both the ``Z`` suffix and explicit numeric offsets. The result is
    always normalized to UTC.
    """
    normalized = text
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp is missing a timezone: {text!r}")
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class LedgerEvent:
    """An immutable, CloudEvents 1.0-aligned ledger event.

    Construct fresh events with :func:`new_event` (which fills in ``id``,
    ``time``, and the constant fields); use :meth:`with_seq` to attach the
    per-machine sequence number at append time. Instances are frozen — every
    mutation returns a new value, preserving the append-only invariant.
    """

    source: str
    type: str
    machine: str
    id: str
    time: str
    specversion: str = SPEC_VERSION
    datacontenttype: str = DEFAULT_CONTENT_TYPE
    data: Any | None = None
    seq: int | None = None

    def with_seq(self, seq: int) -> LedgerEvent:
        """Return a copy of this event with ``seq`` set (original unchanged)."""
        return replace(self, seq=seq)

    def to_dict(self) -> dict[str, Any]:
        """Render the event as a CloudEvents JSON-serializable dict.

        Optional attributes (``data``, ``seq``) are omitted when ``None`` so
        the emitted object stays minimal and spec-clean.
        """
        out: dict[str, Any] = {
            "specversion": self.specversion,
            "id": self.id,
            "source": self.source,
            "type": self.type,
            "time": self.time,
            "datacontenttype": self.datacontenttype,
        }
        if self.data is not None:
            out["data"] = self.data
        out["machine"] = self.machine
        if self.seq is not None:
            out["seq"] = self.seq
        return out

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> LedgerEvent:
        """Reconstruct a :class:`LedgerEvent` from a CloudEvents dict.

        Required context attributes (see ``_REQUIRED_FIELDS``) must be present;
        optional attributes fall back to their defaults.

        Raises:
            ValueError: if a required attribute is missing.
        """
        missing = [k for k in _REQUIRED_FIELDS if k not in payload]
        if missing:
            raise ValueError(f"event is missing required field(s): {', '.join(missing)}")
        return cls(
            source=payload["source"],
            type=payload["type"],
            machine=payload["machine"],
            id=payload["id"],
            time=payload["time"],
            specversion=payload.get("specversion", SPEC_VERSION),
            datacontenttype=payload.get("datacontenttype", DEFAULT_CONTENT_TYPE),
            data=payload.get("data"),
            seq=payload.get("seq"),
        )

    def to_jsonl_line(self) -> str:
        """Serialize the event to a single JSONL line (no trailing newline).

        Uses ``ensure_ascii=False`` so non-ASCII payloads stay human-readable
        in the plain-text ledger files. ``separators`` are compact.
        """
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_jsonl_line(cls, line: str) -> LedgerEvent:
        """Parse one JSONL line back into a :class:`LedgerEvent`."""
        return cls.from_dict(json.loads(line))


def new_event(
    *,
    source: str,
    type: str,
    machine: str,
    data: Any | None = None,
    datacontenttype: str = DEFAULT_CONTENT_TYPE,
    id: str | None = None,
    time: str | None = None,
) -> LedgerEvent:
    """Create a new :class:`LedgerEvent`, filling in generated defaults.

    ``id`` defaults to a fresh uuid4 hex string and ``time`` to the current
    UTC instant (``Z``-suffixed). ``seq`` is left ``None`` for the store to
    assign at append time. Pass ``id`` / ``time`` explicitly for
    deterministic construction (e.g. in tests or re-ingestion).
    """
    return LedgerEvent(
        source=source,
        type=type,
        machine=machine,
        id=id if id is not None else uuid.uuid4().hex,
        time=time if time is not None else format_time(now_utc()),
        datacontenttype=datacontenttype,
        data=data,
    )
