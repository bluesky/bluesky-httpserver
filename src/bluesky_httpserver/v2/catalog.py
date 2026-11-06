"""Operations clients may submit, and validation of their parameters."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterable, Mapping
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
    plan_name : str
        Plan the worker runs for this operation; never shown to clients.
    devices : mapping
        For each device parameter, the device ids clients may name, each mapped to the worker's name for that
        device.
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
    plan_name: str
    devices: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
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
                    "plan_name": self.plan_name,
                    "devices": self.devices,
                }
            )
        ).hexdigest()
        object.__setattr__(self, "sha256", digest)

    def plan_kwargs(self, parameters: Mapping[str, Any]) -> dict[str, Any]:
        """Return the plan's keyword arguments: ``parameters`` with device ids replaced by the worker's names.

        Parameters
        ----------
        parameters : mapping
            Parameters that passed `Catalog.validate` for this descriptor.

        Returns
        -------
        dict
        """
        kwargs = dict(parameters)
        for name, names in self.devices.items():
            if name not in kwargs:
                continue
            value = kwargs[name]
            kwargs[name] = names[value] if isinstance(value, str) else [names[v] for v in value]
        return kwargs


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

    def descriptor(self, operation_id: str, operation_version: str) -> OperationDescriptor:
        """Return the descriptor of an operation.

        Raises
        ------
        UnknownOperation
            The operation is not in the catalog.
        """
        entry = self._entries.get((operation_id, operation_version))
        if entry is None:
            raise UnknownOperation(operation_id, operation_version)
        return entry[0]

    def validate(
        self, operation_id: str, operation_version: str, parameters: Mapping[str, Any]
    ) -> OperationDescriptor:
        """Check that an operation is in the catalog and that ``parameters`` match its schema and devices.

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
            ``parameters`` contain NaN or infinite numbers, do not match the schema, or name a device the
            operation does not accept.
        """
        descriptor = self.descriptor(operation_id, operation_version)
        validator = self._entries[(operation_id, operation_version)][1]
        errors = parameter_errors(validator, descriptor.devices, parameters)
        if errors:
            raise InvalidParameters(errors)
        return descriptor


def parameter_errors(
    validator: Draft202012Validator,
    devices: Mapping[str, Collection[str]],
    parameters: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Return what is wrong with ``parameters`` as ``{"path", "message"}`` dicts; empty if they are valid.

    Parameters
    ----------
    validator : Draft202012Validator
        Validator of the operation's parameters schema.
    devices : mapping
        For each device parameter, the device ids it accepts.
    parameters : mapping
        Decoded JSON parameters object.

    Returns
    -------
    list of dict
        Sorted by path, then message; ``path`` is a JSON path into the parameters.
    """
    try:
        json.dumps(parameters, allow_nan=False)
    except ValueError:
        return [{"path": "$", "message": "parameters must not contain NaN or infinite numbers"}]
    errors = [
        {"path": error.json_path, "message": error.message} for error in validator.iter_errors(dict(parameters))
    ]
    # Device ids are checked only against well-typed values, so a wrongly typed value is reported once.
    if not errors:
        errors = _device_errors(devices, parameters)
    return sorted(errors, key=lambda e: (e["path"], e["message"]))


def _device_errors(devices: Mapping[str, Collection[str]], parameters: Mapping[str, Any]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for name, ids in devices.items():
        if name not in parameters:
            continue
        value = parameters[name]
        if isinstance(value, str):
            named = [(f"$.{name}", value)]
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            named = [(f"$.{name}[{i}]", v) for i, v in enumerate(value)]
        else:
            errors.append({"path": f"$.{name}", "message": "must be a device id or a list of device ids"})
            continue
        expected = ", ".join(sorted(ids))
        errors.extend(
            {"path": path, "message": f"'{v}' is not a device of this operation; expected one of: {expected}"}
            for path, v in named
            if v not in ids
        )
    return errors
