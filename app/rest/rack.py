"""NICo REST v2.3 rack surface (NICo Flow): Rack, Tray (Compute / NVSwitch /
PowerShelf), NVLink domain, rack/tray health reports and leak status,
validation, power (incl. ACPowerCycle → AI-Infra Chassis.Reset), firmware
update, plus the emulator's firmware baseline/drift extension and fault hooks.
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Response

from .. import aiinfra
from ..store import STORE
from . import inventory
from .common import PREFIX, err, now, page
from .core import _ai, _report_entry

router = APIRouter(prefix=PREFIX, tags=["nico-rest-rack"])
hooks = APIRouter(prefix="/emulator/v1/rest", tags=["nico-rest-emulator-hooks"])

POWER_STATES = {"on": "On", "off": "Off", "cycle": "Cycle", "forceoff": "ForceOff",
                "forcecycle": "ForceCycle", "acpowercycle": "ACPowerCycle"}
REDFISH_RESET = {"On": "On", "Off": "GracefulShutdown", "Cycle": "GracefulRestart",
                 "ForceOff": "ForceOff", "ForceCycle": "ForceRestart"}


def _rack(rid: str) -> Dict[str, Any]:
    r = inventory.inv()["racks"].get(rid)
    if not r:
        err(404, f"rack {rid} not found")
    return r


def _tray(tid: str) -> Dict[str, Any]:
    t = inventory.inv()["trays"].get(tid)
    if not t:
        err(404, f"tray {tid} not found")
    return t


def _refresh_leak(t: Dict[str, Any]) -> None:
    """Pull LeakDetection from AI-Infra Redfish for one tray (best effort)."""
    if t["type"] != "Compute":
        return
    try:
        th = aiinfra.thermal(t["id"]) or {}
    except aiinfra.AIInfraError:
        return
    leak = th.get("LeakDetection") or {}
    if isinstance(leak, str):           # e.g. "LeakDetection": "Critical"
        health = leak
    else:
        health = ((leak.get("Status") or {}).get("Health") or leak.get("Health")
                  or leak.get("State") or leak.get("Status"))
    if health in ("Warning", "Critical"):
        _set_leak(t, "Leaking", "redfish")
    elif health == "OK" and t.get("_leak_source") == "redfish":
        _set_leak(t, "NoLeak", "redfish")


def _set_leak(t: Dict[str, Any], status: str, source: str) -> None:
    if status not in ("Leaking", "NoLeak", "Unknown"):
        err(400, "leakStatus must be Leaking, NoLeak or Unknown")
    if t["leakStatus"] != status:
        STORE.event("critical" if status == "Leaking" else "info",
                    "NICo.2.3.TrayLeakStatus", [t["id"], status])
    t["leakStatus"] = status
    t["_leak_source"] = source
    # Flow forces a shutdown of a leaking compute tray
    t["leakHandlingStatus"] = "Down" if status == "Leaking" else "None"
    if status == "Leaking":
        t["powerState"] = "Off"
        t["operationStatus"] = "Error"
    elif t["operationStatus"] == "Error":
        t["operationStatus"] = "Ready"
        t["powerState"] = "On"


def _power_state(body: Dict[str, Any]) -> str:
    st = POWER_STATES.get(str(body.get("state", "")).lower())
    if not st:
        err(400, f"unsupported power state {body.get('state')}")
    return st


def _tray_power(t: Dict[str, Any], state: str) -> Dict[str, Any]:
    if t["type"] == "Compute":
        if state == "ACPowerCycle":
            res = _ai(aiinfra.chassis_reset, t["id"], "PowerCycle")
        else:
            res = _ai(aiinfra.reset_power, t["id"], REDFISH_RESET[state])
    else:
        res = {"delegated": False, "detail": f"{t['type']} power is NICo-local"}
    t["powerState"] = "Off" if state in ("Off", "ForceOff") else "On"
    STORE.event("warning" if state == "ACPowerCycle" else "info",
                "NICo.2.3.TrayPower", [t["id"], state])
    return {"id": t["id"], "state": state, **res}


def _validation(trays: list) -> Dict[str, Any]:
    diffs = []
    drifted = {d["trayId"]: d["diffs"] for d in inventory.drift()}
    for t in trays:
        if t["operationStatus"] == "Error" or t["leakStatus"] == "Leaking":
            diffs.append({"componentId": t["id"], "type": t["type"], "field": "operationStatus",
                          "expected": "Ready", "actual": t["operationStatus"],
                          "kind": "Mismatch"})
        for x in drifted.get(t["id"], []):
            diffs.append({"componentId": t["id"], "type": t["type"],
                          "field": f"firmware.{x['component']}",
                          "expected": x["expected"], "actual": x["actual"],
                          "kind": "Mismatch"})
    return {"diffs": diffs, "totalDiffs": len(diffs), "missingCount": 0,
            "unexpectedCount": 0, "mismatchCount": len(diffs),
            "matchCount": len(trays) - len({d["componentId"] for d in diffs})}


# ── rack ──────────────────────────────────────────────────────────────
@router.get("/rack")
def list_racks(response: Response, siteId: Optional[str] = None,
               includeComponents: bool = False, pageNumber: int = 1, pageSize: int = 20):
    sid = inventory.site_id_of(siteId)
    items = [r for r in inventory.inv()["racks"].values() if not sid or r["_siteId"] == sid]
    return [inventory.rack_view(r, deep=includeComponents)
            for r in page(response, items, pageNumber, pageSize)]


@router.get("/rack/validation")
def validate_racks(siteId: Optional[str] = None):
    sid = inventory.site_id_of(siteId)
    trays = [t for t in inventory.inv()["trays"].values() if not sid or t["_siteId"] == sid]
    return _validation(trays)


@router.get("/rack/{id}")
def get_rack(id: str):
    r = _rack(id)
    for t in inventory.rack_trays(id):
        _refresh_leak(t)
    return inventory.rack_view(r)


@router.get("/rack/{id}/health-report")
def get_rack_reports(id: str):
    r = _rack(id)
    v = inventory.rack_view(r, deep=False)
    agg = {"source": "aggregate-rack-health", "mode": "Observed", "observedAt": now(),
           "successes": [], "alerts": v["health"]["alerts"] + [
               {"id": "Leak", "target": t, "message": "Leak detected",
                "classifications": ["PreventAllocations"]}
               for t in v["health"]["leakingTrays"]]}
    return list(r["_reports"].values()) + [agg]


@router.put("/rack/{id}/health-report")
def put_rack_report(id: str, body: Dict[str, Any] = Body(...)):
    r = _rack(id)
    e = _report_entry(body)
    r["_reports"][e["source"]] = e
    STORE.event("warning" if e["alerts"] else "info", "NICo.2.3.RackHealthReport",
                [id, e["source"], len(e["alerts"])])
    return e


@router.delete("/rack/{id}/health-report/{source}", status_code=202)
def delete_rack_report(id: str, source: str):
    if _rack(id)["_reports"].pop(source, None) is None:
        err(404, f"health report {source} not found")
    return {"message": "Rack health report deletion request was accepted"}


@router.get("/rack/{id}/validation")
def validate_rack(id: str):
    _rack(id)
    return _validation(inventory.rack_trays(id))


@router.patch("/rack/{id}/power")
def rack_power(id: str, body: Dict[str, Any] = Body(...)):
    _rack(id)
    state = _power_state(body)
    trays = [t for t in inventory.rack_trays(id) if t["type"] == "Compute"]
    results = [_tray_power(t, state) for t in trays]
    return {"id": id, "state": state, "trays": len(results),
            "delegated": sum(1 for r in results if r.get("delegated"))}


def _apply_firmware(trays, body: Dict[str, Any]) -> Dict[str, Any]:
    base = inventory.inv()["baseline"]
    updated = []
    for t in trays:
        target = dict(base.get(t["type"], {}))
        comps = body.get("targets")
        if isinstance(comps, list) and comps:
            target = {c: v for c, v in target.items() if c in comps}
        if body.get("version") and not target:
            target = {k: body["version"] for k in t["_fw"]}
        if any(t["_fw"].get(c) != v for c, v in target.items()):
            t["_fw"].update(target)
            updated.append(t["id"])
    STORE.event("info", "NICo.2.3.FirmwareUpdate", [len(updated)])
    return {"updated": updated, "count": len(updated), "status": "Completed"}


@router.patch("/rack/{id}/firmware")
def rack_firmware(id: str, body: Dict[str, Any] = Body(...)):
    _rack(id)
    return {"id": id, **_apply_firmware(inventory.rack_trays(id), body)}


# ── tray ──────────────────────────────────────────────────────────────
@router.get("/tray")
def list_trays(response: Response, siteId: Optional[str] = None,
               rackId: Optional[str] = None, type: Optional[str] = None,
               id: Optional[str] = None, slotId: Optional[int] = None,
               pageNumber: int = 1, pageSize: int = 20):
    sid = inventory.site_id_of(siteId)
    if rackId:
        _rack(rackId)
        items = inventory.rack_trays(rackId)
    else:
        items = list(inventory.inv()["trays"].values())
    items = [t for t in items if (not sid or t["_siteId"] == sid)
             and (not type or t["type"] == type) and (not id or t["id"] == id)
             and (slotId is None or t["position"]["slotId"] == slotId)]
    return [inventory.tray_view(t) for t in page(response, items, pageNumber, pageSize)]


@router.get("/tray/validation")
def validate_trays(siteId: Optional[str] = None, rackId: Optional[str] = None):
    sid = inventory.site_id_of(siteId)
    trays = [t for t in inventory.inv()["trays"].values()
             if (not sid or t["_siteId"] == sid) and (not rackId or t["rackId"] == rackId)]
    return _validation(trays)


@router.get("/tray/{id}")
def get_tray(id: str):
    t = _tray(id)
    _refresh_leak(t)
    return inventory.tray_view(t)


@router.get("/tray/{id}/health-report")
def get_tray_reports(id: str):
    t = _tray(id)
    h = inventory.tray_health(t)
    return list(t["_reports"].values()) + [{"source": h["source"], "mode": "Observed",
                                            "observedAt": h["observedAt"], "successes": [],
                                            "alerts": h["alerts"]}]


@router.put("/tray/{id}/health-report")
def put_tray_report(id: str, body: Dict[str, Any] = Body(...)):
    t = _tray(id)
    e = _report_entry(body)
    t["_reports"][e["source"]] = e
    return e


@router.delete("/tray/{id}/health-report/{source}", status_code=202)
def delete_tray_report(id: str, source: str):
    if _tray(id)["_reports"].pop(source, None) is None:
        err(404, f"health report {source} not found")
    return {"message": "Tray health report deletion request was accepted"}


@router.get("/tray/{id}/validation")
def validate_tray(id: str):
    return _validation([_tray(id)])


@router.get("/tray/{id}/power")
def get_tray_power(id: str):
    t = _tray(id)
    return {"id": id, "powerState": t["powerState"]}


@router.patch("/tray/{id}/power")
def tray_power(id: str, body: Dict[str, Any] = Body(...)):
    return _tray_power(_tray(id), _power_state(body))


@router.patch("/tray/{id}/firmware")
def tray_firmware(id: str, body: Dict[str, Any] = Body(...)):
    return {"id": id, **_apply_firmware([_tray(id)], body)}


# ── NVLink domain ─────────────────────────────────────────────────────
def _domain_view(d: Dict[str, Any]) -> Dict[str, Any]:
    v = {k: val for k, val in d.items() if not k.startswith("_")}
    trays = inventory.inv()["trays"]
    bad = [c["id"] for c in d["components"] if inventory.tray_health(trays[c["id"]])["alerts"]]
    v.update(siteId=d["_siteId"], rackIds=[d["_rackId"]],
             operationStatus="Degraded" if bad else d["operationStatus"],
             health={"status": "Warning" if bad else "OK", "unhealthyComponents": bad},
             gpus=sum(4 for c in d["components"] if c["type"] == "Compute"))
    return v


@router.get("/domain/nvlink")
def list_nvlink_domains(response: Response, siteId: Optional[str] = None,
                        rackId: Optional[str] = None, pageNumber: int = 1, pageSize: int = 20):
    sid = inventory.site_id_of(siteId)
    items = [d for d in inventory.inv()["domains"].values()
             if (not sid or d["_siteId"] == sid) and (not rackId or d["_rackId"] == rackId)]
    return [_domain_view(d) for d in page(response, items, pageNumber, pageSize)]


@router.get("/domain/nvlink/{id}")
def get_nvlink_domain(id: str):
    d = inventory.inv()["domains"].get(id)
    if not d:
        err(404, f"NVLink domain {id} not found")
    return _domain_view(d)


# ── firmware baseline / drift (emulator extension) ────────────────────
@router.get("/firmware/baseline")
def get_baseline():
    return {"baseline": inventory.inv()["baseline"], "dsxOs": "2026.05", "nico": "2.3.0"}


@router.put("/firmware/baseline")
def put_baseline(body: Dict[str, Any] = Body(...)):
    base = inventory.inv()["baseline"]
    for kind, comps in (body.get("baseline") or body).items():
        if kind not in base or not isinstance(comps, dict):
            err(400, f"unknown tray type {kind}")
        base[kind].update({k: str(v) for k, v in comps.items()})
    STORE.event("info", "NICo.2.3.FirmwareBaselineUpdated", [])
    return {"baseline": base}


@router.get("/firmware/drift")
def get_drift(siteId: Optional[str] = None, rackId: Optional[str] = None):
    items = inventory.drift(inventory.site_id_of(siteId), rackId)
    return {"total": len(items), "trays": items, "observedAt": now()}


# ── emulator fault hooks (outside the NICo prefix) ────────────────────
@hooks.post("/trays/{id}/leak")
def inject_leak(id: str, body: Dict[str, Any] = Body(...)):
    t = _tray(id)
    _set_leak(t, body.get("leakStatus", "Leaking"), "emulator")
    return inventory.tray_view(t)


@hooks.post("/machines/{id}/advance")
def advance_machine(id: str):
    m = inventory.machine(id)
    if not m:
        err(404, f"machine {id} not found")
    nxt = inventory.LIFECYCLE_NEXT.get(inventory.lifecycle(m))
    if not nxt:
        err(400, f"machine is {inventory.lifecycle(m)}; nothing to advance")
    inventory.set_lifecycle(m, nxt)
    return inventory.machine_view(m)
