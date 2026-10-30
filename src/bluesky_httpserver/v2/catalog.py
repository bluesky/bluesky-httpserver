"""Operations clients may submit, and validation of their parameters."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


@dataclass(frozen=True)
class OperationDescriptor:
    """An operation clients may submit, and the parameters it accepts.

    Attributes
    ----------
    operation_id : str
        Names the operation.
    operation_version : str
        Version of the operation; a changed parameter contract gets a new version.
    parameters_schema : mapping
        JSON Schema, draft 2020-12, of the parameters object.
    sha256 : str
        Identifies this descriptor; stored with every operation submitted against it.

    Raises
    ------
    ValueError
        ``parameters_schema`` is not a valid draft 2020-12 schema.
    """

    operation_id: str
    operation_version: str
    parameters_schema: Mapping[str, Any]
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        try:
            Draft202012Validator.check_schema(dict(self.parameters_schema))
        except jsonschema.SchemaError as error:
            raise ValueError(
                f"invalid parameters schema for {self.operation_id} version {self.operation_version}:"
                f" {error.message}"
            ) from None
        digest = hashlib.sha256(
            _canonical_json(
                {
                    "operation_id": self.operation_id,
                    "operation_version": self.operation_version,
                    "parameters_schema": self.parameters_schema,
                }
            )
        ).hexdigest()
        object.__setattr__(self, "sha256", digest)


class UnknownOperation(Exception):
    """The catalog has no operation with this identifier and version."""

    def __init__(self, operation_id: str, operation_version: str):
        self.operation_id = operation_id
        self.operation_version = operation_version
        super().__init__(f"operation {operation_id} version {operation_version} is not in the catalog")


class InvalidParameters(Exception):
    """Parameters do not match the operation's schema.

    Attributes
    ----------
    errors : list of dict
        One ``{"path": ..., "message": ...}`` per violation; ``path`` is a JSON path into the parameters.
    """

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__("parameters do not match the operation's schema")


class Catalog:
    """The operations clients may submit; immutable.

    Parameters
    ----------
    descriptors : iterable of OperationDescriptor
        Each ``(operation_id, operation_version)`` at most once.

    Attributes
    ----------
    descriptors : tuple of OperationDescriptor
        Sorted by operation identifier, then version.
    sha256 : str
        Identifies the whole catalog; changes whenever any descriptor is added, removed or changed.

    Raises
    ------
    ValueError
        An ``(operation_id, operation_version)`` pair is repeated.
    """

    def __init__(self, descriptors: Iterable[OperationDescriptor]) -> None:
        ordered = sorted(descriptors, key=lambda d: (d.operation_id, d.operation_version))
        self._entries: dict[tuple[str, str], tuple[OperationDescriptor, Draft202012Validator]] = {}
        for descriptor in ordered:
            key = (descriptor.operation_id, descriptor.operation_version)
            if key in self._entries:
                raise ValueError(f"duplicate operation {key[0]} version {key[1]}")
            self._entries[key] = (descriptor, Draft202012Validator(dict(descriptor.parameters_schema)))
        self.descriptors: tuple[OperationDescriptor, ...] = tuple(ordered)
        self.sha256: str = hashlib.sha256(
            _canonical_json(
                [
                    {
                        "operation_id": d.operation_id,
                        "operation_version": d.operation_version,
                        "descriptor_sha256": d.sha256,
                    }
                    for d in self.descriptors
                ]
            )
        ).hexdigest()

    def validate(
        self, operation_id: str, operation_version: str, parameters: Mapping[str, Any]
    ) -> OperationDescriptor:
        """Check that an operation is in the catalog and that ``parameters`` match its schema.

        Parameters
        ----------
        operation_id, operation_version : str
            Operation to look up.
        parameters : mapping
            Decoded JSON parameters object.

        Returns
        -------
        OperationDescriptor
            The descriptor the parameters were validated against.

        Raises
        ------
        UnknownOperation
            The operation is not in the catalog.
        InvalidParameters
            ``parameters`` contain NaN or infinite numbers, or do not match the schema.
        """
        entry = self._entries.get((operation_id, operation_version))
        if entry is None:
            raise UnknownOperation(operation_id, operation_version)
        descriptor, validator = entry
        try:
            json.dumps(parameters, allow_nan=False)
        except ValueError:
            raise InvalidParameters(
                [{"path": "$", "message": "parameters must not contain NaN or infinite numbers"}]
            ) from None
        errors: list[dict[str, str]] = sorted(
            (
                {"path": error.json_path, "message": error.message}
                for error in validator.iter_errors(dict(parameters))
            ),
            key=lambda e: (e["path"], e["message"]),
        )
        if errors:
            raise InvalidParameters(errors)
        return descriptor
