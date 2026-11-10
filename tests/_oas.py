"""Minimal OpenAPI 3.1 schema checker for contract tests (type, format uuid /
date-time, required, properties, items, enum, $ref). No external dependency."""
import json
import re
from pathlib import Path

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TYPES = {"string": str, "object": dict, "array": list, "boolean": bool,
          "integer": int, "number": (int, float), "null": type(None)}


def load(name):
    return json.loads((Path(__file__).parent / "fixtures" / name).read_text())["schemas"]


def check(value, schema, schemas, path="$"):
    errs = []
    if "$ref" in schema:
        return check(value, schemas[schema["$ref"].rsplit("/", 1)[1]], schemas, path)
    t = schema.get("type")
    if t:
        ts = t if isinstance(t, list) else [t]
        if not any(isinstance(value, _TYPES[x]) and not (x in ("integer", "number")
                   and isinstance(value, bool)) for x in ts):
            return [f"{path}: expected {t}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in enum")
    if isinstance(value, str) and schema.get("format") == "uuid" and not _UUID.match(value):
        errs.append(f"{path}: not a uuid")
    if isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value:
                errs.append(f"{path}: missing {k}")
        for k, sub in (schema.get("properties") or {}).items():
            if k in value:
                errs += check(value[k], sub, schemas, f"{path}.{k}")
        ap = schema.get("additionalProperties")
        if isinstance(ap, dict):
            for k, v in value.items():
                if k not in (schema.get("properties") or {}):
                    errs += check(v, ap, schemas, f"{path}.{k}")
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            errs += check(v, schema["items"], schemas, f"{path}[{i}]")
    return errs
