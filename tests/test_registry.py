"""Unit tests for :mod:`evledger.registry` — the optional type registry."""

from __future__ import annotations

import pytest

from evledger import (
    SchemaValidationError,
    TypeRegistry,
    new_event,
)


def _schema() -> dict[str, object]:
    return {
        "required": ["mission"],
        "properties": {
            "mission": {"type": "string"},
            "count": {"type": "integer"},
        },
    }


def test_unregistered_type_validates_as_freeform() -> None:
    reg = TypeRegistry()
    event = new_event(source="/m/s", type="not.registered", machine="m", data={"x": 1})

    # No schema registered -> always valid, no exception.
    reg.validate(event)


def test_is_registered_reflects_registration() -> None:
    reg = TypeRegistry()
    assert reg.is_registered("dev.example.mission") is False

    reg.register("dev.example.mission", _schema())

    assert reg.is_registered("dev.example.mission") is True


def test_registered_type_accepts_conforming_data() -> None:
    reg = TypeRegistry()
    reg.register("dev.example.mission", _schema())
    event = new_event(
        source="/m/s",
        type="dev.example.mission",
        machine="m",
        data={"mission": "ledger", "count": 3},
    )

    reg.validate(event)  # no exception


def test_missing_required_field_is_rejected() -> None:
    reg = TypeRegistry()
    reg.register("dev.example.mission", _schema())
    event = new_event(
        source="/m/s",
        type="dev.example.mission",
        machine="m",
        data={"count": 3},
    )

    with pytest.raises(SchemaValidationError) as exc:
        reg.validate(event)
    assert "mission" in str(exc.value)


def test_wrong_type_field_is_rejected() -> None:
    reg = TypeRegistry()
    reg.register("dev.example.mission", _schema())
    event = new_event(
        source="/m/s",
        type="dev.example.mission",
        machine="m",
        data={"mission": "ledger", "count": "three"},
    )

    with pytest.raises(SchemaValidationError):
        reg.validate(event)


def test_registered_type_with_none_data_when_required_is_rejected() -> None:
    reg = TypeRegistry()
    reg.register("dev.example.mission", _schema())
    event = new_event(source="/m/s", type="dev.example.mission", machine="m")

    with pytest.raises(SchemaValidationError):
        reg.validate(event)


def test_is_valid_returns_bool_without_raising() -> None:
    reg = TypeRegistry()
    reg.register("dev.example.mission", _schema())
    good = new_event(
        source="/m/s", type="dev.example.mission", machine="m", data={"mission": "x"}
    )
    bad = new_event(source="/m/s", type="dev.example.mission", machine="m", data={})

    assert reg.is_valid(good) is True
    assert reg.is_valid(bad) is False


def test_re_register_overwrites_schema() -> None:
    reg = TypeRegistry()
    reg.register("t", {"required": ["a"]})
    reg.register("t", {"required": ["b"]})
    event = new_event(source="/m/s", type="t", machine="m", data={"b": 1})

    reg.validate(event)  # conforms to the second schema


def test_schema_with_no_constraints_accepts_any_data() -> None:
    reg = TypeRegistry()
    reg.register("t", {})
    event = new_event(source="/m/s", type="t", machine="m", data={"anything": True})

    reg.validate(event)


def test_type_field_constraint_supports_multiple_json_types() -> None:
    reg = TypeRegistry()
    reg.register(
        "t",
        {"properties": {"flag": {"type": "boolean"}, "ratio": {"type": "number"}}},
    )
    event = new_event(
        source="/m/s", type="t", machine="m", data={"flag": True, "ratio": 1.5}
    )

    reg.validate(event)
