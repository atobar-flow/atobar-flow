"""Schema extraction utilities for MCP tools from Python functions."""

import inspect
from typing import Any, get_args, get_origin


def python_type_to_json_schema(py_type: Any) -> dict[str, Any]:
    """Maps Python types and typing constructs to JSON schema dicts."""
    if py_type is None or py_type is inspect.Parameter.empty:
        return {"type": "string"}

    origin = get_origin(py_type)
    args = get_args(py_type)

    if origin is list or (origin is not None and getattr(origin, "__name__", "") == "List"):
        item_type = args[0] if args else Any
        return {"type": "array", "items": python_type_to_json_schema(item_type)}

    if origin is dict or (origin is not None and getattr(origin, "__name__", "") == "Dict"):
        return {"type": "object"}

    # Handle Union / Optional
    if origin is not None and getattr(origin, "__name__", "") == "Union":
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return python_type_to_json_schema(non_none[0])
        return {"anyOf": [python_type_to_json_schema(a) for a in non_none]}

    if py_type is str:
        return {"type": "string"}
    if py_type is int:
        return {"type": "integer"}
    if py_type is float:
        return {"type": "number"}
    if py_type is bool:
        return {"type": "boolean"}
    if py_type is dict:
        return {"type": "object"}
    if py_type is list:
        return {"type": "array"}

    return {"type": "string"}


def extract_parameters_schema(func: Any) -> dict[str, Any]:
    """Builds a JSON Schema object for a callable's parameters."""
    sig = inspect.signature(func)
    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue

        prop_schema = python_type_to_json_schema(param.annotation)
        properties[name] = prop_schema

        if param.default is inspect.Parameter.empty:
            required.append(name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }
