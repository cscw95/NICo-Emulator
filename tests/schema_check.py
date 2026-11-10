"""Minimal JSON Schema checker for upstream contract tests (no jsonschema in the
venv). Covers what the AsyncAPI / OpenAPI documents use: $ref (local), type,
required, properties, additionalProperties, enum, const, oneOf, anyOf, allOf,
items, minimum/maximum. Returns a list of error strings (empty = valid)."""
from typing import Any, Dict, List

_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool,
          "null": type(None)}


def _type_ok(v: Any, t: str) -> bool:
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, _TYPES.get(t, object))


def validate(v: Any, schema: Dict[str, Any], root: Dict[str, Any], path: str = "$") -> List[str]:
    if "$ref" in schema:
        node: Any = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        return validate(v, node, root, path)
    errs: List[str] = []
    t = schema.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        if not any(_type_ok(v, x) for x in ts):
            return [f"{path}: expected {t}, got {type(v).__name__}"]
    if "enum" in schema and v not in schema["enum"]:
        errs.append(f"{path}: {v!r} not in {schema['enum']}")
    if "const" in schema and v != schema["const"]:
        errs.append(f"{path}: {v!r} != const {schema['const']!r}")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if "minimum" in schema and v < schema["minimum"]:
            errs.append(f"{path}: {v} < minimum")
        if "maximum" in schema and v > schema["maximum"]:
            errs.append(f"{path}: {v} > maximum")
    if isinstance(v, dict):
        for k in schema.get("required", []):
            if k not in v:
                errs.append(f"{path}: missing required {k!r}")
        props = schema.get("properties", {})
        for k, sub in props.items():
            if k in v:
                errs += validate(v[k], sub, root, f"{path}.{k}")
        ap = schema.get("additionalProperties")
        extra = [k for k in v if k not in props]
        if ap is False and extra:
            errs.append(f"{path}: additional properties {extra}")
        elif isinstance(ap, dict):
            for k in extra:
                errs += validate(v[k], ap, root, f"{path}.{k}")
    if isinstance(v, list) and isinstance(schema.get("items"), dict):
        for i, x in enumerate(v):
            errs += validate(x, schema["items"], root, f"{path}[{i}]")
    for sub in schema.get("allOf", []):
        errs += validate(v, sub, root, path)
    if "anyOf" in schema and not any(not validate(v, s, root, path) for s in schema["anyOf"]):
        errs.append(f"{path}: matches no anyOf branch")
    if "oneOf" in schema:
        n = sum(1 for s in schema["oneOf"] if not validate(v, s, root, path))
        if n != 1:
            errs.append(f"{path}: matches {n} oneOf branches (want 1)")
    return errs
