"""Generic spec-shaped CRUD for simple NICo REST resources.

``register(router, ...)`` adds ``GET/POST /{path}`` and
``GET/PATCH/DELETE /{path}/{id}`` with the spec's status codes, pagination
and error shape. Hooks let a resource fill defaults or veto a delete.
"""
from typing import Any, Callable, Dict, Iterable, Optional

from fastapi import APIRouter, Body, Request, Response

from . import inventory
from .common import err, history, new_obj, objs, page, public

Hook = Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]]


def register(router: APIRouter, kind: str, path: str, *,
             required: Iterable[str] = ("name", "siteId"),
             filters: Iterable[str] = ("siteId", "tenantId"),
             status: str = "Ready", updatable: Optional[Iterable[str]] = None,
             methods: Iterable[str] = ("get", "patch", "delete"),
             on_create: Hook = None, on_delete: Hook = None,
             tenant_scoped: bool = True) -> None:
    required, filters = tuple(required), tuple(filters)
    methods = tuple(methods)

    @router.get(f"/{path}", name=f"list-{kind}")
    def _list(request: Request, response: Response,
              pageNumber: int = 1, pageSize: int = 20):
        q = request.query_params
        items = list(objs(kind).values())
        for f in filters:
            want = q.get(f)
            if want:
                if f == "siteId":
                    want = inventory.site_id_of(want)
                items = [o for o in items if o.get(f) == want]
        if q.get("query"):
            items = [o for o in items if q["query"].lower() in str(o.get("name", "")).lower()]
        items.sort(key=lambda o: o.get("created", ""), reverse=True)
        return [public(o) for o in page(response, items, pageNumber, pageSize)]

    @router.post(f"/{path}", status_code=201, name=f"create-{kind}")
    def _create(request: Request, body: Dict[str, Any] = Body(...)):
        missing = [f for f in required if body.get(f) in (None, "")]
        if missing:
            err(400, f"missing required field(s): {', '.join(missing)}")
        if "siteId" in body:
            s = inventory.site(body["siteId"])
            if not s:
                err(400, f"site {body['siteId']} not found")
            body["siteId"] = s["id"]
        if tenant_scoped and not body.get("tenantId"):
            body["tenantId"] = request.headers.get("x-tenant-id") or "default"
        obj = new_obj(kind, body, status)
        if body.get("id"):                # spec allows client-provided ids
            obj["id"] = body["id"]
        if obj["id"] in objs(kind):
            err(409, f"{kind} {obj['id']} already exists")
        if on_create:
            on_create(obj, body)
        objs(kind)[obj["id"]] = obj
        return public(obj)

    def _get_obj(oid: str) -> Dict[str, Any]:
        o = objs(kind).get(oid)
        if not o:
            err(404, f"{kind} {oid} not found")
        return o

    if "get" in methods:
        @router.get(f"/{path}/{{oid}}", name=f"get-{kind}")
        def _get(oid: str):
            return public(_get_obj(oid))

    if "patch" in methods:
        @router.patch(f"/{path}/{{oid}}", name=f"update-{kind}")
        def _patch(oid: str, body: Dict[str, Any] = Body(...)):
            o = _get_obj(oid)
            for k, v in body.items():
                if k in ("id", "created", "statusHistory", "status"):
                    continue
                if updatable is not None and k not in updatable:
                    continue
                o[k] = v
            history(o, o["status"], f"{kind} updated")
            return public(o)

    if "delete" in methods:
        @router.delete(f"/{path}/{{oid}}", status_code=202, name=f"delete-{kind}")
        def _delete(oid: str):
            o = _get_obj(oid)
            if on_delete:
                on_delete(o, {})
            objs(kind).pop(oid, None)
            return {"message": f"{kind} deletion request was accepted"}

