from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


SUPPORTED_EVENT_SCHEMA_VERSION = 1


class EventContractError(ValueError):
    """Raised when an event envelope cannot be parsed."""


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EventContractError(f"{path} must be a JSON object")
    return value


def _required(data: Mapping[str, object], key: str, path: str) -> object:
    if key not in data:
        raise EventContractError(f"{path}.{key} is required")
    return data[key]


def _string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise EventContractError(f"{path} must be a non-empty string")
    return value


def _integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EventContractError(f"{path} must be an integer")
    return value


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    event_id: str
    event_type: str
    schema_version: int
    trace_id: str
    run_id: str
    revision_id: str
    sequence: int
    occurred_at: str
    payload: Mapping[str, object]

    @classmethod
    def from_dict(cls, value: object, path: str = "event") -> EventEnvelope:
        data = _mapping(value, path)
        schema_version = _integer(
            _required(data, "schema_version", path),
            f"{path}.schema_version",
        )
        if schema_version != SUPPORTED_EVENT_SCHEMA_VERSION:
            raise EventContractError(
                f"{path}.schema_version must be {SUPPORTED_EVENT_SCHEMA_VERSION}; "
                f"received {schema_version}"
            )
        payload = _mapping(_required(data, "payload", path), f"{path}.payload")
        return cls(
            event_id=_string(_required(data, "event_id", path), f"{path}.event_id"),
            event_type=_string(
                _required(data, "event_type", path),
                f"{path}.event_type",
            ),
            schema_version=schema_version,
            trace_id=_string(_required(data, "trace_id", path), f"{path}.trace_id"),
            run_id=_string(_required(data, "run_id", path), f"{path}.run_id"),
            revision_id=_string(
                _required(data, "revision_id", path),
                f"{path}.revision_id",
            ),
            sequence=_integer(
                _required(data, "sequence", path),
                f"{path}.sequence",
            ),
            occurred_at=_string(
                _required(data, "occurred_at", path),
                f"{path}.occurred_at",
            ),
            payload=dict(payload),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "revision_id": self.revision_id,
            "sequence": self.sequence,
            "occurred_at": self.occurred_at,
            "payload": dict(self.payload),
        }
