"""Shared helpers for the NICo REST surface (``/v2/org/{org}/nico``).

Conventions follow ``dsx-ai-factory/infra-controller`` rest-api/openapi/spec.yaml:
camelCase fields, list = JSON array + ``X-Pagination`` header, POST → 201,
DELETE → 202, errors ``{"source": "nico", "message": ..., "data": null}``.
See docs/nico-rest-paths.md.
"""
import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Response

PREFIX = "/v2/org/{org}/nico"
DEFAULT_ORG = "neocloud"
PROVIDER_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "nico/provider/neocloud"))
MAX_PAGE = 100
# DoS guard for the in-memory store: max created objects per kind.
MAX_OBJECTS = int(os.environ.get("NICO_MAX_OBJECTS", "10000"))


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def uid(kind: str, key: Optional[str] = None) -> str:
    """Deterministic UUID for seeded objects, random for created ones."""
    if key is not None:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"nico/{kind}/{key}"))
    return str(uuid.uuid4())


def err(code: int, message: str):
    raise HTTPException(code, {"source": "nico", "message": message, "data": None})


def history(obj: Dict[str, Any], status: str, message: str = "") -> None:
    obj.setdefault("statusHistory", []).append(
        {"status": status, "message": message or status, "created": now(),
         "updated": now()})
    obj["status"] = status
    obj["updated"] = now()


def page(response: Response, items: List[Any], page_number: int = 1,
         page_size: int = 20, order_by: str = "CREATED_DESC") -> List[Any]:
    """Slice a list per spec pagination and set the X-Pagination header."""
    page_size = max(1, min(page_size or 20, MAX_PAGE))
    page_number = max(1, page_number or 1)
    start = (page_number - 1) * page_size
    response.headers["X-Pagination"] = json.dumps(
        {"pageNumber": page_number, "pageSize": page_size,
         "total": len(items), "orderBy": order_by})
    return items[start:start + page_size]


def public(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Strip internal (underscore) keys."""
    return {k: v for k, v in obj.items() if not k.startswith("_")}


def new_obj(kind: str, body: Dict[str, Any], status: str = "Ready",
            **extra) -> Dict[str, Any]:
    if len(OBJ.get(kind, {})) >= MAX_OBJECTS:
        err(503, f"{kind} limit ({MAX_OBJECTS}) reached")
    ts = now()
    obj = {"id": uid(kind), **{k: v for k, v in body.items() if v is not None},
           **extra, "created": ts, "updated": ts, "statusHistory": []}
    history(obj, status, f"{kind} created")
    return obj


# created objects, by kind ("vpc", "subnet", "instance", ...) -> id -> object
OBJ: Dict[str, Dict[str, Dict[str, Any]]] = {}


def objs(kind: str) -> Dict[str, Dict[str, Any]]:
    return OBJ.setdefault(kind, {})


def reset_objs() -> None:
    OBJ.clear()
