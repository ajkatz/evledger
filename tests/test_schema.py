"""Unit tests for :mod:`evledger.schema` — the CloudEvents envelope."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from evledger import (
    LedgerEvent,
    format_time,
    new_event,
    now_utc,
    parse_time,
)

# --- time helpers ---------------------------------------------------------


def test_format_time_renders_utc_with_z_suffix() -> None:
    dt = datetime(2026, 5, 29, 14, 30, 15, tzinfo=timezone.utc)

    assert format_time(dt) == "2026-05-29T14:30:15Z"


def test_format_time_includes_microseconds_when_present() -> None:
    dt = datetime(2026, 5, 29, 14, 30, 15, 123456, tzinfo=timezone.utc)

    assert format_time(dt) == "2026-05-29T14:30:15.123456Z"


def test_format_time_converts_aware_non_utc_to_utc() -> None:
    from datetime import timedelta

    eastern = timezone(timedelta(hours=-5))
    dt = datetime(2026, 5, 29, 9, 30, 15, tzinfo=eastern)

    # 09:30 -05:00 == 14:30 UTC
    assert format_time(dt) == "2026-05-29T14:30:15Z"


def test_format_time_rejects_naive_datetime() -> None:
    naive = datetime(2026, 5, 29, 14, 30, 15)

    with pytest.raises(ValueError):
        format_time(naive)


def test_parse_time_reads_z_suffix() -> None:
    dt = parse_time("2026-05-29T14:30:15Z")

    assert dt == datetime(2026, 5, 29, 14, 30, 15, tzinfo=timezone.utc)


def test_parse_time_reads_microseconds() -> None:
    dt = parse_time("2026-05-29T14:30:15.123456Z")

    assert dt == datetime(2026, 5, 29, 14, 30, 15, 123456, tzinfo=timezone.utc)


def test_parse_time_reads_numeric_offset() -> None:
    dt = parse_time("2026-05-29T09:30:15-05:00")

    assert dt == parse_time("2026-05-29T14:30:15Z")


def test_format_then_parse_roundtrips() -> None:
    dt = datetime(2026, 5, 29, 14, 30, 15, 7, tzinfo=timezone.utc)

    assert parse_time(format_time(dt)) == dt


def test_now_utc_is_timezone_aware_utc() -> None:
    now = now_utc()

    assert now.tzinfo == timezone.utc


# --- LedgerEvent construction ---------------------------------------------


def test_new_event_populates_cloudevents_fields() -> None:
    event = new_event(
        source="/laptop/dev",
        type="dev.example.thing.start",
        machine="laptop",
        data={"k": "v"},
    )

    assert event.specversion == "1.0"
    assert event.source == "/laptop/dev"
    assert event.type == "dev.example.thing.start"
    assert event.machine == "laptop"
    assert event.data == {"k": "v"}
    assert event.datacontenttype == "application/json"
    assert event.seq is None
    # id is a uuid4 hex (32 hex chars, no dashes)
    assert re.fullmatch(r"[0-9a-f]{32}", event.id)
    # time is ISO-8601 UTC with Z suffix
    assert event.time.endswith("Z")
    parse_time(event.time)  # parses without error


def test_new_event_generates_unique_ids() -> None:
    e1 = new_event(source="/m/s", type="t", machine="m")
    e2 = new_event(source="/m/s", type="t", machine="m")

    assert e1.id != e2.id


def test_new_event_accepts_explicit_time_and_id() -> None:
    event = new_event(
        source="/m/s",
        type="t",
        machine="m",
        id="deadbeef" * 4,
        time="2026-05-29T14:30:15Z",
    )

    assert event.id == "deadbeef" * 4
    assert event.time == "2026-05-29T14:30:15Z"


def test_new_event_defaults_data_to_none() -> None:
    event = new_event(source="/m/s", type="t", machine="m")

    assert event.data is None


def test_ledger_event_is_frozen() -> None:
    event = new_event(source="/m/s", type="t", machine="m")

    with pytest.raises(Exception):
        event.type = "other"  # type: ignore[misc]


# --- serialization --------------------------------------------------------


def test_to_dict_emits_cloudevents_shape() -> None:
    event = new_event(
        source="/laptop/dev",
        type="dev.example.thing",
        machine="laptop",
        id="a" * 32,
        time="2026-05-29T14:30:15Z",
        data={"k": "v"},
    )

    assert event.to_dict() == {
        "specversion": "1.0",
        "id": "a" * 32,
        "source": "/laptop/dev",
        "type": "dev.example.thing",
        "time": "2026-05-29T14:30:15Z",
        "datacontenttype": "application/json",
        "data": {"k": "v"},
        "machine": "laptop",
    }


def test_to_dict_includes_seq_when_set() -> None:
    base = new_event(source="/m/s", type="t", machine="m", id="b" * 32)
    event = base.with_seq(7)

    d = event.to_dict()
    assert d["seq"] == 7


def test_to_dict_omits_data_when_none() -> None:
    event = new_event(source="/m/s", type="t", machine="m")

    assert "data" not in event.to_dict()


def test_to_dict_omits_seq_when_none() -> None:
    event = new_event(source="/m/s", type="t", machine="m")

    assert "seq" not in event.to_dict()


def test_with_seq_returns_new_event_leaving_original_unchanged() -> None:
    event = new_event(source="/m/s", type="t", machine="m")

    seqd = event.with_seq(3)

    assert event.seq is None
    assert seqd.seq == 3
    assert seqd.id == event.id


def test_from_dict_roundtrips_to_dict() -> None:
    event = new_event(
        source="/laptop/dev",
        type="dev.example.thing",
        machine="laptop",
        data={"k": [1, 2, 3]},
    ).with_seq(42)

    assert LedgerEvent.from_dict(event.to_dict()) == event


def test_from_dict_tolerates_missing_optional_fields() -> None:
    minimal = {
        "specversion": "1.0",
        "id": "c" * 32,
        "source": "/m/s",
        "type": "t",
        "time": "2026-05-29T14:30:15Z",
        "machine": "m",
    }

    event = LedgerEvent.from_dict(minimal)

    assert event.data is None
    assert event.seq is None
    assert event.datacontenttype == "application/json"


def test_from_dict_rejects_missing_required_field() -> None:
    bad = {"specversion": "1.0", "id": "x" * 32, "source": "/m/s"}

    with pytest.raises((KeyError, ValueError)):
        LedgerEvent.from_dict(bad)


def test_to_jsonl_line_is_single_line_valid_json() -> None:
    event = new_event(
        source="/m/s",
        type="t",
        machine="m",
        data={"nested": {"a": 1}},
    ).with_seq(1)

    line = event.to_jsonl_line()

    assert "\n" not in line
    assert json.loads(line) == event.to_dict()


def test_to_jsonl_line_preserves_non_ascii() -> None:
    event = new_event(source="/m/s", type="t", machine="m", data={"name": "café"})

    line = event.to_jsonl_line()

    # ensure_ascii=False keeps the character readable in the file
    assert "café" in line
    assert json.loads(line)["data"]["name"] == "café"


def test_from_jsonl_line_roundtrips() -> None:
    event = new_event(source="/m/s", type="t", machine="m", data={"k": "v"}).with_seq(5)

    assert LedgerEvent.from_jsonl_line(event.to_jsonl_line()) == event
