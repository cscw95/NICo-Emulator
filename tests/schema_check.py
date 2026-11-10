"""Minimal schema validator for contract tests (the venv has no jsonschema).

Covers what the upstream copies in tests/contracts use: JSON Schema 2020-12
($ref to #/$defs, type/type-list, const, enum, required, properties,
additionalProperties, propertyNames, items, minLength, minimum) and Kubernetes
CRD openAPIV3Schema (nullable, x-kubernetes-preserve-unknown-fields; unknown
fields are an error, as the API server would prune them)."""
from typing import Any, List

_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _is(v: Any, t: str) -> bool:
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, _TYPES[t])


def validate(value: Any, schema: dict, root: dict = None, path: str = "$", crd: bool = False) -> List[str]:
    root = root or schema
    errs: List[str] = []
    if "$ref" in schema:
        node = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        return validate(value, node, root, path, crd)
    if value is None and schema.get("nullable"):
        return errs
    t = schema.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        if not any(_is(value, x) for x in ts):
            return [f"{path}: expected {t}, got {type(value).__name__}"]
    if "const" in schema and value != schema["const"]:
        errs.append(f"{path}: expected const {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        errs.append(f"{path}: shorter than {schema['minLength']}")
    if isinstance(value, (int, float)) and "minimum" in schema and value < schema["minimum"]:
        errs.append(f"{path}: below minimum {schema['minimum']}")
    if isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value:
                errs.append(f"{path}: missing required '{k}'")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties")
        for k, v in value.items():
            if "propertyNames" in schema:
                errs += validate(k, schema["propertyNames"], root, f"{path}.<key {k}>", crd)
            if k in props:
                errs += validate(v, props[k], root, f"{path}.{k}", crd)
            elif isinstance(extra, dict):
                errs += validate(v, extra, root, f"{path}.{k}", crd)
            elif extra is False or (crd and props and extra is None
                                    and not schema.get("x-kubernetes-preserve-unknown-fields")):
                errs.append(f"{path}: unknown field '{k}'")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(value):
            errs += validate(v, schema["items"], root, f"{path}[{i}]", crd)
    return errs
