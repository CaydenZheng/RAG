"""Bounded JSON Schema validation for untrusted external tool definitions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

MAX_TOOL_SCHEMA_BYTES = 64 * 1024
MAX_TOOL_SCHEMA_DEPTH = 32


class InvalidToolSchema(ValueError):
    """Raised when a tool schema is invalid or exceeds host safety limits."""


def _measure_depth(value: object) -> int:
    """Measure nested mapping/list depth without recursive Python calls."""
    maximum = 1
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        maximum = max(maximum, depth)
        if isinstance(current, Mapping):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return maximum


def _schema_children(
    resource: Resource[Any],
    resolver: Any,
) -> list[tuple[Resource[Any], Any]]:
    children = [
        (subresource, resolver.in_subresource(subresource))
        for subresource in resource.subresources()
    ]
    contents = resource.contents
    if isinstance(contents, Mapping):
        for keyword in ("$ref", "$dynamicRef"):
            reference = contents.get(keyword)
            if reference is None:
                continue
            if not isinstance(reference, str) or not reference.startswith("#"):
                raise InvalidToolSchema("external schema references are not allowed")
            try:
                resolved = resolver.lookup(reference)
            except Unresolvable as exc:
                raise InvalidToolSchema(
                    "local schema reference could not be resolved"
                ) from exc
            target = Resource.from_contents(
                resolved.contents,
                default_specification=DRAFT202012,
            )
            children.append((target, resolved.resolver))
    return children


def _validate_local_references(schema: dict[str, Any]) -> None:
    resource_uri = "urn:ragrag:tool-input-schema"
    resource = Resource.from_contents(
        schema,
        default_specification=DRAFT202012,
    )
    registry = Registry().with_resource(resource_uri, resource).crawl()
    resolver = registry.resolver(resource_uri)
    states: dict[int, int] = {}
    stack: list[tuple[Resource[Any], Any, bool]] = [
        (resource, resolver, False)
    ]
    while stack:
        current, current_resolver, exiting = stack.pop()
        marker = id(current.contents)
        if exiting:
            states[marker] = 2
            continue
        state = states.get(marker, 0)
        if state == 1:
            raise InvalidToolSchema(
                "circular local schema references are not allowed"
            )
        if state == 2:
            continue
        states[marker] = 1
        stack.append((current, current_resolver, True))
        children = _schema_children(current, current_resolver)
        stack.extend(
            (child, child_resolver, False)
            for child, child_resolver in reversed(children)
        )


def ensure_safe_input_schema(schema: dict[str, Any]) -> None:
    """Validate one MCP input schema before registering its tool."""
    if not isinstance(schema, dict):
        raise InvalidToolSchema("tool input schema must be an object")
    try:
        serialized = json.dumps(
            schema,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError) as exc:
        raise InvalidToolSchema("tool input schema must be JSON serializable") from exc
    if len(serialized) > MAX_TOOL_SCHEMA_BYTES:
        raise InvalidToolSchema("tool input schema exceeds the size limit")
    if _measure_depth(schema) > MAX_TOOL_SCHEMA_DEPTH:
        raise InvalidToolSchema("tool input schema exceeds the nesting limit")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise InvalidToolSchema("tool input schema is not valid JSON Schema") from exc
    except Exception as exc:
        raise InvalidToolSchema(
            "tool input schema could not be checked safely"
        ) from exc
    try:
        _validate_local_references(schema)
    except InvalidToolSchema:
        raise
    except Exception as exc:
        raise InvalidToolSchema(
            "tool input schema references could not be checked safely"
        ) from exc


def validate_tool_arguments(
    schema: dict[str, Any], arguments: object
) -> str | None:
    """Return a stable, value-free error for arguments rejected by a schema."""
    try:
        validator = Draft202012Validator(schema)
        error = next(validator.iter_errors(arguments), None)
    except Exception:
        return "Tool parameters could not be validated safely"
    if error is None:
        return None
    path = "$"
    if error.absolute_path:
        path += "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
    rule = str(error.validator or "schema")
    return f"Tool parameters do not match the schema at {path} (rule: {rule})"
