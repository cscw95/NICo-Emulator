"""NICo REST core: metadata, provider/tenant, Site, Allocation(+Constraint),
InstanceType, ExpectedMachine, Machine (+ power / chassis reset / GPU reset /
health report / validation), Instance, OperatingSystem, SSH keys.

Instances share state with the deprecated ``/nico-bridge``: creating one runs
the bridge allocation (AI-Infra DPU tenant attachment) and the instance id is
the bridge instance id, so either surface can release it.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Request, Response

from .. import __version__, aiinfra, bridge
from ..store import STORE
from . import crud, inventory
from .common import (PREFIX, PROVIDER_ID, err, history, new_obj, now, objs,
                     page, public, uid)


VALIDATION_TESTS = [
    ("DcgmFullShort", "DCGM diag level 2", "dcgmi", "diag -r 2"),
    ("NvlinkBandwidth", "NVLink bandwidth check", "nvbandwidth", "-t device_to_device"),
    ("IbLoopback", "InfiniBand / Spectrum-X loopback", "ib_write_bw", "--report_gbits"),
    ("NcclAllReduce", "NCCL all-reduce in NVL72 domain", "all_reduce_perf", "-b 8 -e 8G"),
]
POWER_ACTIONS = {"On": "On", "GracefulShutdown": "GracefulShutdown", "ForceOff": "ForceOff",
                 "GracefulRestart": "GracefulRestart", "ForceRestart": "ForceRestart"}


def _ai(call, *a, **k) -> Dict[str, Any]:
    """Best-effort AI-Infra call → {delegated, ...}."""
    try:
        return {"delegated": True, "result": call(*a, **k)}
    except aiinfra.AIInfraError as e:
        STORE.event("warning", "NICo.2.3.AIInfraCallSkipped", [str(e)[:160]])
        return {"delegated": False, "detail": str(e)[:200]}


def seed_catalog() -> None:
    """Seeded InstanceTypes + OperatingSystems (idempotent)."""
    its, oss = objs("instance-type"), objs("operating-system")
    if its:
        return
    ts = now()
    for s in inventory.inv()["sites"].values():
        for sku, desc, caps in (
                ("vr-nvl72", "Vera Rubin NVL72 compute tray (4x Rubin GPU, 1x BlueField-4)",
                 [{"type": "GPU", "name": "NVIDIA Rubin", "count": 4},
                  {"type": "DPU", "name": "BlueField-4", "count": 1}]),
                ("cpu-epyc", "General purpose CPU node (control plane)",
                 [{"type": "CPU", "name": "AMD EPYC", "count": 2}])):
            iid = uid("instance-type", f"{s['id']}/{sku}")
            its[iid] = {"id": iid, "name": f"{sku}", "description": desc,
                        "controllerMachineType": sku, "infrastructureProviderId": PROVIDER_ID,
                        "siteId": s["id"], "labels": {"sku": sku}, "machineCapabilities": caps,
                        "status": "Ready", "statusHistory": [], "deprecations": [],
                        "created": ts, "updated": ts}
    for name, url in (("ubuntu-24.04-dgx", "https://images.neocloud.example/ubuntu-24.04-dgx.qcow2"),
                      ("nvke-node-1.34", "https://images.neocloud.example/nvke-node-1.34.qcow2")):
        oid = uid("operating-system", name)
        oss[oid] = {"id": oid, "name": name, "description": f"{name} image",
                    "infrastructureProviderId": PROVIDER_ID, "tenantId": None, "type": "Image",
                    "imageUrl": url, "isCloudInit": True, "phoneHomeEnabled": False,
                    "isActive": True, "allowOverride": True,
                    "siteAssociations": [{"siteId": s} for s in inventory.inv()["sites"]],
                    "status": "Ready", "statusHistory": [], "created": ts, "updated": ts}


router = APIRouter(prefix=PREFIX, tags=["nico-rest-core"],
                   dependencies=[Depends(seed_catalog)])


def _machines() -> Dict[str, Dict[str, Any]]:
    return inventory.inv()["machines"]


def _machine(mid: str) -> Dict[str, Any]:
    m = inventory.machine(mid)
    if not m:
        err(404, f"machine {mid} not found")
    return m


# ── metadata / provider / tenant ──────────────────────────────────────
@router.get("/metadata")
def metadata():
    return {"version": "2.3.0", "buildTime": "2026-10-31T00:00:00Z",
            "emulator": {"name": "NICo Emulator", "version": __version__}}


@router.get("/infrastructure-provider/current")
def provider_current(org: str):
    return {"id": PROVIDER_ID, "org": org, "orgDisplayName": "NeoCloud",
            "name": "NeoCloud", "created": STORE.started, "updated": STORE.started}


@router.get("/tenant/current")
def tenant_current(org: str, request: Request):
    tid = request.headers.get("x-tenant-id") or "default"
    return {"id": tid, "org": org, "orgDisplayName": tid,
            "tenantContact": None, "created": STORE.started, "updated": STORE.started}


# ── site ──────────────────────────────────────────────────────────────
def _site_stats(sid: str) -> Dict[str, Any]:
    by: Dict[str, int] = {}
    total = 0
    for m in _machines().values():
        if m["siteId"] == sid:
            st = inventory.LIFECYCLE_STATUS[inventory.lifecycle(m)]
            by[st] = by.get(st, 0) + 1
            total += 1
    return {"total": total, "byStatus": by}


def _site_view(s: Dict[str, Any], stats: bool = False) -> Dict[str, Any]:
    v = public(s)
    v["slug"] = s.get("_slug")
    if stats:
        v["machineStats"] = _site_stats(s["id"])
    return v


@router.get("/site")
def list_sites(response: Response, includeMachineStats: bool = False,
               pageNumber: int = 1, pageSize: int = 20):
    items = list(inventory.inv()["sites"].values())
    return [_site_view(s, includeMachineStats) for s in page(response, items, pageNumber, pageSize)]


@router.post("/site", status_code=201)
def create_site(body: Dict[str, Any] = Body(...)):
    if not body.get("name"):
        err(400, "missing required field(s): name")
    s = new_obj("site", body, "Registered", org="neocloud",
                infrastructureProviderId=PROVIDER_ID, isOnline=True,
                siteControllerVersion="2.3.0", siteAgentVersion="2.3.0")
    inventory.inv()["sites"][s["id"]] = s
    return _site_view(s)


def _site(site_id: str) -> Dict[str, Any]:
    s = inventory.site(site_id)
    if not s:
        err(404, f"site {site_id} not found")
    return s


@router.get("/site/{siteId}")
def get_site(siteId: str, includeMachineStats: bool = False):
    return _site_view(_site(siteId), includeMachineStats)


@router.patch("/site/{siteId}")
def update_site(siteId: str, body: Dict[str, Any] = Body(...)):
    s = _site(siteId)
    for k in ("name", "description", "location", "contact", "serialConsoleHostname",
              "isSerialConsoleEnabled"):
        if k in body:
            s[k] = body[k]
    s["updated"] = now()
    return _site_view(s)


@router.delete("/site/{siteId}", status_code=202)
def delete_site(siteId: str):
    s = _site(siteId)
    if any(m["siteId"] == s["id"] for m in _machines().values()):
        err(400, "site still has machines")
    inventory.inv()["sites"].pop(s["id"], None)
    return {"message": "Site deletion request was accepted"}


@router.get("/site/{siteId}/status-history")
def site_status_history(siteId: str):
    return _site(siteId)["statusHistory"]


@router.get("/site/{siteId}/health-probe")
def site_health_probe(siteId: str):
    """Emulator extension: site health probe roll-up (control plane + fleet)."""
    s = _site(siteId)
    ai = aiinfra.ping()
    racks = [r for r in inventory.inv()["racks"].values() if r["_siteId"] == s["id"]]
    rv = [inventory.rack_view(r, deep=False) for r in racks]
    ms = [m for m in _machines().values() if m["siteId"] == s["id"]]
    unhealthy = [m["id"] for m in ms if not inventory.is_healthy(m)]
    leaking = sum(len(r["health"]["leakingTrays"]) for r in rv)
    probes = [
        {"id": "SiteAgentConnected", "status": "OK"},
        {"id": "TemporalWorkflows", "status": "OK"},
        {"id": "AIInfraRedfishReachable", "status": "OK" if ai.get("reachable") else "Critical",
         "detail": ai.get("url")},
        {"id": "RackHealth", "status": "Critical" if any(r["health"]["status"] == "Critical" for r in rv)
         else "OK", "racksCritical": sum(1 for r in rv if r["health"]["status"] == "Critical")},
        {"id": "LeakDetection", "status": "Critical" if leaking else "OK", "leakingTrays": leaking},
        {"id": "MachineHealth", "status": "Warning" if unhealthy else "OK",
         "unhealthyMachines": len(unhealthy)},
        {"id": "FirmwareBaseline", "status": "Warning" if inventory.drift(s["id"]) else "OK",
         "driftedTrays": len(inventory.drift(s["id"]))},
    ]
    worst = "Critical" if any(p["status"] == "Critical" for p in probes) else (
        "Warning" if any(p["status"] == "Warning" for p in probes) else "OK")
    return {"siteId": s["id"], "siteName": s["name"], "status": worst,
            "observedAt": now(), "probes": probes,
            "racks": len(racks), "machines": len(ms), "machineStats": _site_stats(s["id"])}


# ── allocation (+ constraint) ─────────────────────────────────────────
def _constraints(body_cs: List[Dict[str, Any]], alloc_id: str) -> List[Dict[str, Any]]:
    out = []
    for c in body_cs or []:
        if c.get("resourceType", "InstanceType") == "InstanceType" and \
                c.get("resourceTypeId") not in objs("instance-type"):
            err(400, f"instance type {c.get('resourceTypeId')} not found")
        ts = now()
        out.append({"id": uid("allocation-constraint"), "allocationId": alloc_id,
                    "resourceType": c.get("resourceType", "InstanceType"),
                    "resourceTypeId": c.get("resourceTypeId"),
                    "constraintType": c.get("constraintType", "Reserved"),
                    "constraintValue": int(c.get("constraintValue", 0)),
                    "derivedResourceId": None, "deprecations": [],
                    "created": ts, "updated": ts})
    return out


def _alloc_create(o: Dict[str, Any], body: Dict[str, Any]) -> None:
    seed_catalog()
    o["infrastructureProviderId"] = PROVIDER_ID
    o["allocationConstraints"] = _constraints(body.get("allocationConstraints"), o["id"])
    history(o, "Registered", "Allocation registered")


crud.register(router, "allocation", "allocation", required=("name", "tenantId", "siteId"),
              on_create=_alloc_create, updatable=("name", "description"))


@router.patch("/allocation/{allocationId}/constraint/{allocationConstraintId}")
def update_constraint(allocationId: str, allocationConstraintId: str,
                      body: Dict[str, Any] = Body(...)):
    a = objs("allocation").get(allocationId)
    if not a:
        err(404, f"allocation {allocationId} not found")
    for c in a["allocationConstraints"]:
        if c["id"] == allocationConstraintId:
            if "constraintValue" in body:
                used = _used(a["tenantId"], a["siteId"], c["resourceTypeId"])
                if int(body["constraintValue"]) < used:
                    err(400, f"constraintValue below current usage ({used})")
                c["constraintValue"] = int(body["constraintValue"])
            c["updated"] = now()
            return c
    err(404, f"constraint {allocationConstraintId} not found")


def _used(tenant: str, site_id: str, it_id: str) -> int:
    return sum(1 for i in objs("instance").values()
               if i["tenantId"] == tenant and i["siteId"] == site_id
               and i.get("instanceTypeId") == it_id)


def _quota(tenant: str, site_id: str, it_id: str) -> Optional[int]:
    """Allowed count for tenant/site/type; None = no allocations (lenient)."""
    allocs = [a for a in objs("allocation").values()
              if a["tenantId"] == tenant and a["siteId"] == site_id]
    if not allocs:
        return None
    return sum(c["constraintValue"] for a in allocs for c in a["allocationConstraints"]
               if c["resourceTypeId"] == it_id)


# ── instance type ─────────────────────────────────────────────────────
def _it_view(o: Dict[str, Any]) -> Dict[str, Any]:
    v = public(o)
    ms = [m for m in _machines().values() if m.get("instanceTypeId") == o["id"]]
    assigned = sum(1 for m in ms if inventory.lifecycle(m) == "assigned")
    allocated = sum(c["constraintValue"] for a in objs("allocation").values()
                    for c in a["allocationConstraints"] if c["resourceTypeId"] == o["id"])
    v["allocationStats"] = {"total": len(ms), "used": assigned, "allocated": allocated,
                            "unused": max(0, len(ms) - assigned),
                            "unusedUsable": sum(1 for m in ms if inventory.lifecycle(m) == "ready"
                                                and inventory.is_healthy(m))}
    return v


@router.get("/instance/type")
def list_instance_types(response: Response, siteId: Optional[str] = None,
                        pageNumber: int = 1, pageSize: int = 20):
    seed_catalog()
    sid = inventory.site_id_of(siteId)
    items = [o for o in objs("instance-type").values() if not sid or o["siteId"] == sid]
    return [_it_view(o) for o in page(response, items, pageNumber, pageSize)]


@router.post("/instance/type", status_code=201)
def create_instance_type(body: Dict[str, Any] = Body(...)):
    seed_catalog()
    if not body.get("name") or not body.get("siteId"):
        err(400, "missing required field(s): name, siteId")
    body["siteId"] = _site(body["siteId"])["id"]
    o = new_obj("instance-type", body, "Ready", infrastructureProviderId=PROVIDER_ID,
                deprecations=[])
    o.setdefault("machineCapabilities", [])
    o.setdefault("labels", {})
    objs("instance-type")[o["id"]] = o
    return _it_view(o)


def _it(it_id: str) -> Dict[str, Any]:
    seed_catalog()
    o = objs("instance-type").get(it_id)
    if not o:
        err(404, f"instance type {it_id} not found")
    return o


@router.get("/instance/type/{instanceTypeId}")
def get_instance_type(instanceTypeId: str):
    return _it_view(_it(instanceTypeId))


@router.patch("/instance/type/{instanceTypeId}")
def update_instance_type(instanceTypeId: str, body: Dict[str, Any] = Body(...)):
    o = _it(instanceTypeId)
    for k in ("name", "description", "labels", "machineCapabilities"):
        if k in body:
            o[k] = body[k]
    o["updated"] = now()
    return _it_view(o)


@router.delete("/instance/type/{instanceTypeId}", status_code=202)
def delete_instance_type(instanceTypeId: str):
    _it(instanceTypeId)
    if any(m.get("instanceTypeId") == instanceTypeId for m in _machines().values()):
        err(400, "instance type has associated machines")
    objs("instance-type").pop(instanceTypeId, None)
    return {"message": "Instance Type deletion request was accepted"}


@router.get("/instance/type/{instanceTypeId}/machine")
def list_it_machines(instanceTypeId: str, response: Response,
                     pageNumber: int = 1, pageSize: int = 20):
    _it(instanceTypeId)
    items = [{"id": f"mit-{m['id']}", "machineId": m["id"], "instanceTypeId": instanceTypeId}
             for m in _machines().values() if m.get("instanceTypeId") == instanceTypeId]
    return page(response, items, pageNumber, pageSize)


@router.post("/instance/type/{instanceTypeId}/machine", status_code=201)
def associate_machines(instanceTypeId: str, body: Dict[str, Any] = Body(...)):
    _it(instanceTypeId)
    out = []
    for mid in body.get("machineIds") or []:
        m = _machine(mid)
        m["instanceTypeId"] = instanceTypeId
        out.append({"id": f"mit-{m['id']}", "machineId": m["id"],
                    "instanceTypeId": instanceTypeId})
    return out


# ── expected machine / power shelf ────────────────────────────────────
def _em_create(o: Dict[str, Any], body: Dict[str, Any]) -> None:
    seed_catalog()
    o.pop("defaultBmcPassword", None)
    o.pop("tenantId", None)
    mid = f"nh-{body.get('serverName') or body.get('name') or o['chassisSerialNumber']}"
    if mid in _machines():
        err(409, f"machine {mid} already exists")
    m = inventory._new_machine(mid, o["siteId"], body.get("rackId"), None, "expected")
    m["serialNumber"] = o["chassisSerialNumber"]
    _machines()[mid] = m
    o["machineId"] = mid
    o.setdefault("isDpfEnabled", True)
    o.setdefault("labels", {})


def _em_delete(o: Dict[str, Any], _: Dict[str, Any]) -> None:
    m = _machines().get(o.get("machineId") or "")
    if m and inventory.lifecycle(m) == "assigned":
        err(400, "machine is assigned to an instance")
    _machines().pop(o.get("machineId") or "", None)


crud.register(router, "expected-machine", "expected-machine",
              required=("siteId", "bmcMacAddress", "chassisSerialNumber"),
              filters=("siteId",), on_create=_em_create, on_delete=_em_delete,
              tenant_scoped=False)
crud.register(router, "expected-power-shelf", "expected-power-shelf",
              required=("siteId", "bmcMacAddress"), filters=("siteId",),
              tenant_scoped=False)


# ── machine ───────────────────────────────────────────────────────────
@router.get("/machine")
def list_machines(response: Response, siteId: Optional[str] = None,
                  status: Optional[str] = None, instanceTypeId: Optional[str] = None,
                  tenantId: Optional[str] = None, hasInstance: Optional[bool] = None,
                  rackId: Optional[str] = None, query: Optional[str] = None,
                  pageNumber: int = 1, pageSize: int = 20):
    sid = inventory.site_id_of(siteId)
    items = []
    for m in _machines().values():
        if sid and m["siteId"] != sid:
            continue
        if instanceTypeId and m.get("instanceTypeId") != instanceTypeId:
            continue
        if rackId and m.get("_rackId") != rackId:
            continue
        if query and query not in m["id"]:
            continue
        items.append(m)
    views = []
    need_view = status or tenantId or hasInstance is not None
    if need_view:
        for m in items:
            v = inventory.machine_view(m)
            if status and v["status"] != status:
                continue
            if tenantId and v["tenantId"] != tenantId:
                continue
            if hasInstance is not None and bool(v["instanceId"]) != hasInstance:
                continue
            views.append(v)
        return page(response, views, pageNumber, pageSize)
    return [inventory.machine_view(m) for m in page(response, items, pageNumber, pageSize)]


@router.get("/machine/gpu/stats")
def gpu_stats(siteId: Optional[str] = None):
    sid = inventory.site_id_of(siteId)
    total = used = 0
    for m in _machines().values():
        if sid and m["siteId"] != sid:
            continue
        g = sum(c["count"] for c in m["machineCapabilities"] if c["type"] == "GPU")
        total += g
        if inventory.lifecycle(m) == "assigned":
            used += g
    return [{"name": "NVIDIA Rubin", "gpus": total, "machines": total // 4,
             "used": used, "unused": total - used}]


@router.get("/machine/{machineId}")
def get_machine(machineId: str):
    return inventory.machine_view(_machine(machineId))


@router.patch("/machine/{machineId}")
def update_machine(machineId: str, body: Dict[str, Any] = Body(...)):
    m = _machine(machineId)
    if "instanceTypeId" in body:
        if body["instanceTypeId"] and body["instanceTypeId"] not in objs("instance-type"):
            seed_catalog()
            if body["instanceTypeId"] not in objs("instance-type"):
                err(400, "instance type not found")
        m["instanceTypeId"] = body["instanceTypeId"]
    if "labels" in body:
        m["labels"] = body["labels"] or {}
    if "maintenanceMessage" in body:
        m["maintenanceMessage"] = body["maintenanceMessage"]
        h = bridge._host(m["id"]) if body["maintenanceMessage"] else bridge._hosts.get(m["id"])
        if h is not None:
            h["cordoned"] = bool(body["maintenanceMessage"])
    m["updated"] = now()
    return inventory.machine_view(m)


@router.delete("/machine/{machineId}", status_code=202)
def delete_machine(machineId: str):
    m = _machine(machineId)
    if inventory.lifecycle(m) == "assigned":
        err(400, "machine is assigned to an instance")
    _machines().pop(m["id"], None)
    return {"message": "Machine deletion request was accepted"}


def _assert_ack(m: Dict[str, Any], body: Dict[str, Any]) -> None:
    if inventory.lifecycle(m) == "assigned" and not body.get("acknowledgeAttachedInstance"):
        err(400, "Machine has an attached Instance; set acknowledgeAttachedInstance=true")


@router.patch("/machine/{machineId}/power", status_code=202)
def machine_power(machineId: str, body: Dict[str, Any] = Body(...)):
    m = _machine(machineId)
    action = body.get("action")
    if action not in list(POWER_ACTIONS) + ["ACPowercycle", "ACPowerCycle"]:
        err(400, f"unsupported action {action}")
    _assert_ack(m, body)
    tray = m.get("_trayId") or m["id"]
    if action in ("ACPowercycle", "ACPowerCycle"):
        res = _ai(aiinfra.chassis_reset, tray, "PowerCycle")
        STORE.event("warning", "NICo.2.3.MachineACPowerCycle", [m["id"]])
    else:
        res = _ai(aiinfra.reset_power, tray, POWER_ACTIONS[action])
        STORE.event("info", "NICo.2.3.MachinePowerControl", [m["id"], action])
    history(m, m["status"], f"power {action}")
    return {"message": "Machine power control request was accepted", "action": action, **res}


@router.patch("/machine/{machineId}/chassis/{chassisId}/reset", status_code=202)
def chassis_reset(machineId: str, chassisId: str, body: Optional[Dict[str, Any]] = Body(None)):
    m = _machine(machineId)
    _assert_ack(m, body or {})
    res = _ai(aiinfra.chassis_reset, chassisId, (body or {}).get("resetType", "PowerCycle"))
    STORE.event("warning", "NICo.2.3.ChassisReset", [m["id"], chassisId])
    return {"message": "Machine chassis reset request was accepted", **res}


@router.patch("/machine/{machineId}/bmc/reset", status_code=202)
def bmc_reset(machineId: str, body: Optional[Dict[str, Any]] = Body(None)):
    m = _machine(machineId)
    STORE.event("info", "NICo.2.3.BmcReset", [m["id"]])
    return {"message": "Machine BMC reset request was accepted"}


@router.post("/machine/{machineId}/gpu/reset", status_code=202)
def gpu_reset(machineId: str, body: Optional[Dict[str, Any]] = Body(None)):
    """Emulator extension — OOB AdminGpuReset (NICo v2.3) via AI-Infra Redfish."""
    m = _machine(machineId)
    _assert_ack(m, body or {})
    res = _ai(aiinfra.gpu_reset, m.get("_trayId") or m["id"])
    STORE.event("warning", "NICo.2.3.AdminGpuReset", [m["id"]])
    return {"message": "Machine GPU reset request was accepted", **res}


@router.post("/machine/{machineId}/decommission", status_code=202)
def decommission(machineId: str):
    m = _machine(machineId)
    if inventory.lifecycle(m) == "assigned":
        err(400, "machine is assigned to an instance")
    inventory.set_lifecycle(m, "decommissioned", "decommissioned")
    return {"message": "Machine decommission request was accepted"}


@router.get("/machine/{machineId}/status-history")
def machine_status_history(machineId: str):
    m = _machine(machineId)
    inventory.lifecycle(m)
    return m["statusHistory"]


def _report_entry(body: Dict[str, Any]) -> Dict[str, Any]:
    if not body.get("source") or not body.get("mode"):
        err(400, "missing required field(s): source, mode")
    return {"source": body["source"], "mode": body["mode"],
            "triggeredBy": body.get("triggeredBy", "api"), "observedAt": now(),
            "successes": body.get("successes") or [], "alerts": body.get("alerts") or []}


@router.get("/machine/{machineId}/health-report")
def get_machine_reports(machineId: str):
    m = _machine(machineId)
    return list(m["_reports"].values()) + [inventory.machine_health(m)]


@router.put("/machine/{machineId}/health-report")
def put_machine_report(machineId: str, body: Dict[str, Any] = Body(...)):
    m = _machine(machineId)
    e = _report_entry(body)
    m["_reports"][e["source"]] = e
    STORE.event("warning" if e["alerts"] else "info", "NICo.2.3.MachineHealthReport",
                [m["id"], e["source"], len(e["alerts"])])
    return e


@router.delete("/machine/{machineId}/health-report/{source}", status_code=202)
def delete_machine_report(machineId: str, source: str):
    m = _machine(machineId)
    if m["_reports"].pop(source, None) is None:
        err(404, f"health report {source} not found")
    return {"message": "Machine health report deletion request was accepted"}


@router.get("/machine/{machineId}/validation/run")
def list_validation_runs(machineId: str):
    return _machine(machineId)["_runs"]


@router.post("/machine/{machineId}/validation/run", status_code=202)
def run_validation(machineId: str, body: Optional[Dict[str, Any]] = Body(None)):
    """Run machine validation (DCGM / NVLink / IB / NCCL). Completes
    synchronously; ``body.failTests`` (emulator) forces failures."""
    m = _machine(machineId)
    body = body or {}
    lc = inventory.lifecycle(m)
    if lc == "assigned":
        err(400, "machine is assigned to an instance")
    if lc in ("expected", "discovered"):
        inventory.set_lifecycle(m, "validating", "validation started")
    vid = uid("validation")
    allowed = body.get("allowedTests")
    fail = set(body.get("failTests") or [])
    if not inventory.is_healthy(m):
        fail.add("DcgmFullShort")
    ts = now()
    results = []
    for name, desc, cmd, args in VALIDATION_TESTS:
        if allowed and name not in allowed:
            continue
        bad = name in fail
        results.append({"name": name, "description": desc, "command": cmd, "args": args,
                        "stdOut": "" if bad else f"{name} passed",
                        "stdErr": f"{name} failed" if bad else "",
                        "context": "OnDemand", "exitCode": 1 if bad else 0,
                        "startTime": ts, "endTime": ts, "validationID": vid,
                        "testID": f"forge_{name}"})
    ok = all(r["exitCode"] == 0 for r in results)
    run = {"validationID": vid, "machineID": m["id"], "startTime": ts, "endTime": ts,
           "name": f"Test_{m['id']}", "context": "OnDemand",
           "status": {"state": "Success" if ok else "Failed", "total": len(results),
                      "completed": len(results)},
           "durationToCompleteSecs": 0}
    m["_runs"].append(run)
    m["_results"] = results
    if inventory.lifecycle(m) in ("validating", "failed", "ready"):
        inventory.set_lifecycle(m, "ready" if ok else "failed",
                                "validation passed" if ok else "validation failed")
    STORE.event("info" if ok else "warning", "NICo.2.3.MachineValidation",
                [m["id"], run["status"]["state"]])
    return run


@router.get("/machine/{machineId}/validation/result")
def validation_results(machineId: str):
    return _machine(machineId)["_results"]


# ── instance ──────────────────────────────────────────────────────────
def _pick_machine(body: Dict[str, Any], site_id: str) -> Dict[str, Any]:
    if body.get("machineId"):
        m = _machine(body["machineId"])
        if inventory.lifecycle(m) != "ready":
            err(400, f"machine {m['id']} is not Ready ({inventory.lifecycle(m)})")
        if not body.get("allowUnhealthyMachine") and not inventory.is_healthy(m):
            err(400, f"machine {m['id']} is unhealthy")
        return m
    it_id = body.get("instanceTypeId")
    if not it_id:
        err(400, "instanceTypeId or machineId is required")
    for m in _machines().values():
        if m["siteId"] == site_id and m.get("instanceTypeId") == it_id \
                and inventory.lifecycle(m) == "ready" and m["_lifecycle"] == "ready" \
                and (body.get("allowUnhealthyMachine") or inventory.is_healthy(m)) \
                and not (bridge._hosts.get(m["id"]) or {}).get("cordoned"):
            return m
    err(409, f"no Ready machine of instance type {it_id} at site {site_id}")


def _inst_view(i: Dict[str, Any]) -> Dict[str, Any]:
    v = public(i)
    m = _machines().get(i.get("machineId") or "")
    if m and i["status"] == "Ready" and inventory.lifecycle(m) != "assigned":
        v["status"] = "Error"
    return v


def _bridge_instances(skip: set) -> List[Dict[str, Any]]:
    """Instances allocated through /nico-bridge (shared state)."""
    out = []
    for h in bridge._hosts.values():
        iid = h.get("instance_id")
        if not iid or iid in skip:
            continue
        m = inventory.machine(h["host_id"])
        out.append({"id": iid, "name": iid, "tenantId": h.get("tenant_id") or h.get("tenant_ref"),
                    "infrastructureProviderId": PROVIDER_ID,
                    "siteId": m["siteId"] if m else None,
                    "instanceTypeId": m.get("instanceTypeId") if m else None,
                    "vpcId": None, "machineId": h["host_id"], "status": "Ready",
                    "labels": {"source": "nico-bridge"}, "statusHistory": []})
    return out


@router.get("/instance")
def list_instances(response: Response, siteId: Optional[str] = None,
                   tenantId: Optional[str] = None, vpcId: Optional[str] = None,
                   status: Optional[str] = None, pageNumber: int = 1, pageSize: int = 20):
    sid = inventory.site_id_of(siteId)
    items = [_inst_view(i) for i in objs("instance").values()]
    items += _bridge_instances(set(objs("instance")))
    items = [i for i in items if (not sid or i["siteId"] == sid)
             and (not tenantId or i["tenantId"] == tenantId)
             and (not vpcId or i.get("vpcId") == vpcId)
             and (not status or i["status"] == status)]
    return page(response, items, pageNumber, pageSize)


@router.post("/instance", status_code=201)
def create_instance(request: Request, body: Dict[str, Any] = Body(...)):
    seed_catalog()
    body.setdefault("tenantId", request.headers.get("x-tenant-id"))
    missing = [f for f in ("name", "tenantId", "vpcId") if not body.get(f)]
    if missing:
        err(400, f"missing required field(s): {', '.join(missing)}")
    vpc = objs("vpc").get(body["vpcId"])
    if not vpc:
        err(400, f"vpc {body['vpcId']} not found")
    if body.get("operatingSystemId") and body["operatingSystemId"] not in objs("operating-system"):
        err(400, f"operating system {body['operatingSystemId']} not found")
    for g in body.get("sshKeyGroupIds") or []:
        if g not in objs("sshkeygroup"):
            err(400, f"ssh key group {g} not found")
    site_id = vpc["siteId"]
    m = _pick_machine(body, site_id)
    it_id = body.get("instanceTypeId") or m.get("instanceTypeId")
    quota = _quota(body["tenantId"], site_id, it_id)
    if quota is not None and _used(body["tenantId"], site_id, it_id) + 1 > quota:
        err(409, f"allocation exhausted for instance type {it_id} (limit {quota})")
    hv = bridge.allocate(bridge._InstanceBody(host_id=m["id"], tenant_ref=body["tenantId"]))
    inventory.set_lifecycle(m, "assigned", f"assigned to {hv['instance_id']}")
    ts = now()
    inst = {"id": hv["instance_id"], "name": body["name"], "description": body.get("description"),
            "tenantId": body["tenantId"], "infrastructureProviderId": PROVIDER_ID,
            "siteId": site_id, "instanceTypeId": it_id, "vpcId": vpc["id"],
            "secondaryVpcIds": body.get("secondaryVpcIds") or [], "machineId": m["id"],
            "operatingSystemId": body.get("operatingSystemId"),
            "networkSecurityGroupId": body.get("networkSecurityGroupId") or vpc.get("networkSecurityGroupId"),
            "networkSecurityGroupInherited": not body.get("networkSecurityGroupId"),
            "networkSecurityGroupPropagationDetails": None,
            "powerProfile": body.get("powerProfile"), "controllerInstanceId": hv["instance_id"],
            "ipxeScript": body.get("ipxeScript"), "userData": body.get("userData"),
            "alwaysBootWithCustomIpxe": bool(body.get("alwaysBootWithCustomIpxe")),
            "phoneHomeEnabled": bool(body.get("phoneHomeEnabled")),
            "labels": body.get("labels") or {}, "isUpdatePending": False,
            "serialConsoleUrl": None, "autoNetwork": body.get("autoNetwork", True),
            "interfaces": body.get("interfaces") or [],
            "infinibandInterfaces": body.get("infinibandInterfaces") or [],
            "spectrumXAttachments": body.get("spectrumXAttachments") or [],
            "nvLinkInterfaces": body.get("nvLinkInterfaces") or [],
            "dpuExtensionServiceDeployments": [],
            "sshKeyGroupIds": body.get("sshKeyGroupIds") or [], "sshKeyGroups": [],
            "deprecations": [], "created": ts, "updated": ts, "statusHistory": []}
    history(inst, "Provisioning", "machine assigned")
    history(inst, "Ready", "instance ready")
    objs("instance")[inst["id"]] = inst
    STORE.event("info", "NICo.2.3.InstanceCreated", [inst["id"], m["id"], body["tenantId"]])
    return _inst_view(inst)


def _inst(iid: str) -> Dict[str, Any]:
    i = objs("instance").get(iid)
    if not i:
        for b in _bridge_instances(set()):
            if b["id"] == iid:
                return b
        err(404, f"instance {iid} not found")
    return i


@router.get("/instance/{instanceId}")
def get_instance(instanceId: str):
    i = _inst(instanceId)
    return _inst_view(i) if instanceId in objs("instance") else i


@router.patch("/instance/{instanceId}")
def update_instance(instanceId: str, body: Dict[str, Any] = Body(...)):
    i = objs("instance").get(instanceId) or err(404, f"instance {instanceId} not found")
    for k in ("name", "description", "labels", "userData", "ipxeScript",
              "networkSecurityGroupId", "sshKeyGroupIds", "operatingSystemId",
              "nvLinkInterfaces", "infinibandInterfaces"):
        if k in body:
            i[k] = body[k]
    if body.get("triggerReboot"):
        _ai(aiinfra.reset_power, (inventory.machine(i["machineId"]) or {}).get("_trayId") or i["machineId"],
            "ForceRestart")
    i["updated"] = now()
    return _inst_view(i)


@router.delete("/instance/{instanceId}", status_code=202)
def delete_instance(instanceId: str, body: Optional[Dict[str, Any]] = Body(None)):
    i = _inst(instanceId)
    try:
        bridge.release(instanceId)
    except Exception:                    # already released on the bridge
        pass
    m = inventory.machine(i["machineId"])
    if m:
        inventory.set_lifecycle(m, "sanitizing", f"sanitizing after {instanceId}")
    objs("instance").pop(instanceId, None)
    STORE.event("info", "NICo.2.3.InstanceDeleted", [instanceId])
    return {"message": "Instance deletion request was accepted"}


@router.get("/instance/{instanceId}/status-history")
def instance_status_history(instanceId: str):
    return _inst(instanceId).get("statusHistory", [])


# ── operating system / ssh keys ───────────────────────────────────────
def _os_create(o: Dict[str, Any], body: Dict[str, Any]) -> None:
    o.setdefault("type", "iPXE" if body.get("ipxeScript") else "Image")
    o.setdefault("isActive", True)
    o["siteAssociations"] = [{"siteId": inventory.site_id_of(s)} for s in body.get("siteIds") or []]
    o.pop("siteIds", None)
    o.pop("imageAuthToken", None)


def _skg_create(o: Dict[str, Any], body: Dict[str, Any]) -> None:
    o["org"] = "neocloud"
    o["version"] = "1"
    o["sshKeys"] = [objs("sshkey")[k] for k in body.get("sshKeyIds") or [] if k in objs("sshkey")]
    o["siteAssociations"] = [{"siteId": inventory.site_id_of(s)} for s in body.get("siteIds") or []]
    o.pop("siteIds", None)


def _sk_create(o: Dict[str, Any], body: Dict[str, Any]) -> None:
    if not str(body.get("publicKey", "")).startswith(("ssh-", "ecdsa-")):
        err(400, "publicKey must be an OpenSSH public key")
    o["fingerprint"] = f"SHA256:{uid('fp', body['publicKey'])[:22]}"


crud.register(router, "operating-system", "operating-system", required=("name",),
              filters=("tenantId",), on_create=_os_create)
crud.register(router, "sshkeygroup", "sshkeygroup", required=("name",),
              filters=("tenantId",), on_create=_skg_create)
crud.register(router, "sshkey", "sshkey", required=("name", "publicKey"),
              filters=("tenantId",), on_create=_sk_create, methods=("get", "patch", "delete"))
