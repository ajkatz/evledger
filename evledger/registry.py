"""Optional per-type ``data`` schema registry for ledger events.

An event ``type`` MAY register a schema describing the shape of its ``data``
payload. Registered types are validated on demand; *unregistered* types log
freeform (validation is a no-op). This keeps the common case zero-friction
while letting important event types opt into richer guarantees (Decision 3).

The validator is a deliberately small, stdlib-only subset of JSON Schema —
no third-party dependency — supporting the constraints the ledger needs:

* ``required``: a list of keys that must be present in ``data``.
* ``properties``: a mapping of key -> ``{"type": <json-type>}`` constraints,
  where ``<json-type>`` is one of ``object``, ``array``, ``string``,
  ``integer``, ``number``, ``boolean``, ``null`` (or a list of such names).

Anything not described is unconstrained. Adopters that need full JSON Schema
can layer a richer validator on top; this one favors zero dependencies.

Imports nothing from sibling ``claude_kg`` modules.
"""

from __future__ import annotations

from typing import Any

from evledger.schema import LedgerEvent

#: Mapping from a JSON Schema ``type`` name to the Python type(s) it permits.
#: ``bool`` is checked before ``int`` by the validator because ``bool`` is a
#: subclass of ``int`` in Python.
_JSON_TYPE_CHECKS: dict[str, Any] = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
    "null": type(None),
}


class SchemaValidationError(ValueError):
    """Raised when an event's ``data`` does not conform to its registered schema."""


def _matches_json_type(value: Any, json_type: str) -> bool:
    if json_type == "boolean":
        return isinstance(value, bool)
    if json_type in ("integer", "number"):
        # Exclude bool, which is a subclass of int.
        if isinstance(value, bool):
            return False
        if json_type == "integer":
            return isinstance(value, int)
        return isinstance(value, (int, float))
    expected = _JSON_TYPE_CHECKS.get(json_type)
    if expected is None:
        # Unknown type name -> treat as unconstrained.
        return True
    return isinstance(value, expected)


def _validate_against_schema(data: Any, schema: dict[str, Any]) -> list[str]:
    """Return a list of human-readable validation error strings (empty == ok)."""
    errors: list[str] = []

    required = schema.get("required", [])
    if required and not isinstance(data, dict):
        errors.append(f"data must be an object with required keys {sorted(required)}")
        return errors

    if isinstance(data, dict):
        for key in required:
            if key not in data:
                errors.append(f"missing required field {key!r}")

        properties = schema.get("properties", {})
        for key, constraint in properties.items():
            if key not in data:
                continue
            json_type = constraint.get("type")
            if json_type is None:
                continue
            allowed = [json_type] if isinstance(json_type, str) else list(json_type)
            if not any(_matches_json_type(data[key], t) for t in allowed):
                errors.append(
                    f"field {key!r} must be of type {json_type!r}, "
                    f"got {type(data[key]).__name__}"
                )

    return errors


class TypeRegistry:
    """A registry mapping event ``type`` -> optional ``data`` schema.

    Unregistered types validate freeform. Register a schema with
    :meth:`register`, then check events with :meth:`validate` (raises) or
    :meth:`is_valid` (returns a bool).
    """

    def __init__(self) -> None:
        self._schemas: dict[str, dict[str, Any]] = {}

    def register(self, type: str, data_schema: dict[str, Any]) -> None:
        """Register (or overwrite) the ``data`` schema for an event type."""
        self._schemas[type] = data_schema

    def is_registered(self, type: str) -> bool:
        """Return whether a schema is registered for ``type``."""
        return type in self._schemas

    def schema_for(self, type: str) -> dict[str, Any] | None:
        """Return the registered schema for ``type``, or ``None``."""
        return self._schemas.get(type)

    def validate(self, event: LedgerEvent) -> None:
        """Validate ``event.data`` against its registered schema.

        A no-op for unregistered types (freeform logging).

        Raises:
            SchemaValidationError: if a registered schema is violated.
        """
        schema = self._schemas.get(event.type)
        if schema is None:
            return
        errors = _validate_against_schema(event.data, schema)
        if errors:
            raise SchemaValidationError(
                f"event type {event.type!r} failed validation: " + "; ".join(errors)
            )

    def is_valid(self, event: LedgerEvent) -> bool:
        """Return ``True`` if the event conforms (or its type is unregistered)."""
        try:
            self.validate(event)
            return True
        except SchemaValidationError:
            return False
