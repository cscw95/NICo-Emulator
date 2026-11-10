"""F28 Health Validation rules on the NICo REST surface.

Upstream NICo (infra-controller rest-api/openapi/spec.yaml @ 9ace631fa361) exposes
``/machine/{machineId}/validation/run|result``; the validation run below the REST layer
is ``crates/machine-controller/src/handler/machine_validation.rs``. Rule CRUD does not
exist upstream (health-validation ships rules in its catalog, cc26732a510f), so the rule
and verdict routes here are a NeoCloud extension under ``x-neocloud/`` and marked with
``x-neocloud`` in the OpenAPI and an ``X-NeoCloud-Extension`` response header.

  GET    /x-neocloud/health-validation/rules              list (X-Pagination)
  GET    /x-neocloud/health-validation/rules/{name}
  PUT    /x-neocloud/health-validation/rules/{name}       create or replace {name, expr, sensors, ...}
  DELETE /x-neocloud/health-validation/rules/{name}       202
  POST   /x-neocloud/health-validation/rules:evaluate     dry run {expr|rule, machineId|observations}
  GET    /machine/{machineId}/x-neocloud/observations     current readings (baseline + injected)
  GET    /machine/{machineId}/x-neocloud/health-verdict   NodeVerdict (schema 0.5)
Emulator hooks: PUT|DELETE /emulator/v1/rest/machines/{id}/observations (fault injection).

``POST /machine/{id}/validation/run`` (app/rest/core.py) appends one
MachineValidationResult per enabled rule (testID ``neocloud_hv_<rule>``); a CRITICAL
or UNKNOWN rule fails the run, which fails acceptance in the machine lifecycle.
"""
import copy
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Response

from .. import health_validation as hv
from ..store import STORE
from . import inventory
from .common import PREFIX, err, page

X = {"x-neocloud": True}
router = APIRouter(prefix=PREFIX, tags=["nico-x-neocloud-health-validation"])
hooks = APIRouter(prefix="/emulator/v1/rest", tags=["nico-rest-emulator-hooks"])

_STATE: Dict[str, Any] = {"rules": None, "overrides": {}, "verdicts": {}}


def reset() -> None:
    _STATE.update(rules=None, overrides={}, verdicts={})


def rules() -> Dict[str, Dict[str, Any]]:
    if _STATE["rules"] is None:
        _STATE["rules"] = hv.seed_rules()
    return _STATE["rules"]


def _mark(response: Optional[Response]) -> None:
    if response is not None:
        response.headers["X-NeoCloud-Extension"] = "health-validation-rules"


def _bad(e: hv.RuleError):
    err(400, str(e)) if e.data is None else _raise_data(str(e), e.data)


def _raise_data(message: str, data: dict):
    from fastapi import HTTPException
    raise HTTPException(400, {"source": "nico", "message": message, "data": data})


def _machine(mid: str) -> Dict[str, Any]:
    m = inventory.machine(mid)
    if m is None:
        err(404, f"machine {mid} not found")
    return m


def observations(m: Dict[str, Any]) -> Dict[str, Any]:
    tray = inventory.inv()["trays"].get(m.get("_trayId") or "") or {}
    gpus = next((c["count"] for c in m.get("machineCapabilities") or [] if c["type"] == "GPU"), 0)
    alerts = [a for a in inventory.machine_health(m)["alerts"] if a.get("id") != "Leak"]
    healthy = not any("PreventAllocations" in (a.get("classifications") or []) for a in alerts)
    base = hv.baseline_observations(m["id"], gpus, healthy=healthy,
                                    leak=tray.get("leakStatus") == "Leaking")
    return hv.apply_overrides(base, _STATE["overrides"].get(m["id"]) or {})


def verdict(m: Dict[str, Any], trace_id: Optional[str] = None) -> Dict[str, Any]:
    v = hv.node_verdict(m["id"], list(rules().values()), observations(m), trace_id)
    _STATE["verdicts"][m["id"]] = v
    STORE.event("info" if hv.ready(v) else "warning", "NICo.2.3.HealthVerdict",
                [m["id"], v["overall_severity"]])
    return v


def run_results(m: Dict[str, Any], vid: str, ts: str, allowed: Optional[List[str]]) -> List[Dict[str, Any]]:
    """MachineValidationResult rows (spec.yaml components) for one validation run."""
    v = verdict(m, trace_id=uuid.UUID(vid).hex if _is_uuid(vid) else None)
    out = []
    for r in v.get("check_results") or []:
        tid = f"neocloud_hv_{r['check_id']}"
        if allowed and tid not in allowed and "HealthValidation" not in allowed:
            continue
        bad = r["severity"] in ("UNKNOWN", "CRITICAL")
        line = f"{r['check_id']} severity={r['severity']}"
        out.append({"name": f"HealthValidation/{r['check_id']}",
                    "description": rules()[r["check_id"]].get("description") or r["check_id"],
                    "command": "health-validation", "args": f"check --id {r['check_id']}",
                    "stdOut": "" if bad else line,
                    "stdErr": "; ".join(r.get("messages") or [line]) if bad else "",
                    "context": "OnDemand", "exitCode": 1 if bad else 0,
                    "startTime": ts, "endTime": ts, "validationID": vid, "testID": tid})
    return out


def _is_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
        return True
    except ValueError:
        return False


@router.get("/x-neocloud/health-validation/rules", openapi_extra=X)
def list_rules(response: Response, pageNumber: int = 1, pageSize: int = 100):
    _mark(response)
    return page(response, list(rules().values()), pageNumber, pageSize, "NAME_ASC")


@router.post("/x-neocloud/health-validation/rules:evaluate", openapi_extra=X)
def evaluate(response: Response, body: Dict[str, Any] = Body(...)):
    """Dry run: {expr} or {rule:{...}} against {machineId} or explicit {observations}."""
    _mark(response)
    try:
        rule = hv.normalize_rule(body.get("rule") or {"name": "dry-run", "expr": body.get("expr"),
                                                      "sensors": body.get("sensors")})
    except hv.RuleError as e:
        _bad(e)
    if body.get("machineId"):
        obs = observations(_machine(body["machineId"]))
    elif isinstance(body.get("observations"), dict):
        o = body["observations"]
        obs = {"node": dict(o.get("node") or {}), "gpus": list(o.get("gpus") or [])}
    else:
        err(400, "machineId or observations is required")
    return hv.evaluate_rule(rule, obs, uuid.uuid4().hex)


@router.get("/x-neocloud/health-validation/rules/{name}", openapi_extra=X)
def get_rule(name: str, response: Response):
    _mark(response)
    if name not in rules():
        err(404, f"rule {name} not found")
    return rules()[name]


@router.put("/x-neocloud/health-validation/rules/{name}", openapi_extra=X)
def put_rule(name: str, response: Response, body: Dict[str, Any] = Body(...)):
    """Create or replace. Replacing a seeded rule keeps its catalog defaults, so a changed
    threshold shows up as ``overridden`` in the verdict."""
    _mark(response)
    base = rules().get(name)
    try:
        rule = hv.normalize_rule(body, name, {"defaults": base["defaults"],
                                              "nvidiaEndorsed": base.get("nvidiaEndorsed", False)}
                                 if base else None)
    except hv.RuleError as e:
        _bad(e)
    if base is None and len(rules()) >= 256:
        err(400, "too many rules (max 256)")
    rules()[name] = rule
    STORE.event("info", "NICo.2.3.HealthRuleChanged", [name, "replaced" if base else "created"])
    response.status_code = 200 if base else 201
    return rule


@router.delete("/x-neocloud/health-validation/rules/{name}", status_code=202, openapi_extra=X)
def delete_rule(name: str, response: Response):
    _mark(response)
    if rules().pop(name, None) is None:
        err(404, f"rule {name} not found")
    return {"message": f"rule {name} deleted"}


@router.get("/machine/{machineId}/x-neocloud/observations", openapi_extra=X)
def get_observations(machineId: str, response: Response):
    _mark(response)
    m = _machine(machineId)
    return {"machineId": m["id"], "observedAt": hv.now(), "signals": hv.SIGNALS,
            **observations(m), "overrides": copy.deepcopy(_STATE["overrides"].get(m["id"]) or {})}


@router.get("/machine/{machineId}/x-neocloud/health-verdict", openapi_extra=X)
def get_verdict(machineId: str, response: Response):
    _mark(response)
    return verdict(_machine(machineId))


@hooks.put("/machines/{id}/observations")
def inject_observations(id: str, body: Dict[str, Any] = Body(...)):
    """Fault injection: {signals:{DCGM_FI_DEV_XID_ERROR: 79}, gpu?: 0}."""
    m = _machine(id)
    try:
        _STATE["overrides"][m["id"]] = hv.merge_override(_STATE["overrides"].get(m["id"]), body)
    except hv.RuleError as e:
        _bad(e)
    STORE.event("warning", "NICo.2.3.ObservationInjected", [m["id"], sorted(body["signals"])])
    return {"machineId": m["id"], "overrides": _STATE["overrides"][m["id"]]}


@hooks.delete("/machines/{id}/observations")
def clear_observations(id: str):
    m = _machine(id)
    _STATE["overrides"].pop(m["id"], None)
    return {"machineId": m["id"], "overrides": {}}


@hooks.post("/health-validation/reset")
def reset_hook():
    reset()
    return {"rules": len(rules()), "overrides": 0}
