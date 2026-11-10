"""NICo REST bring-up surface (F23/F25):

  POST /expected-rack {siteId, rackId, name, labels}   upstream pre-registration
  GET  /expected-rack · /expected-rack/{id}
  POST /rack/{id}/bringup {siteId}           upstream Flow bring-up → {taskIds}
  POST /rack {name, siteId, serial}          v2 contract alias: register a new rack (18 compute + 9
                                             NVSwitch trays + 4 power shelves, one
                                             NVLink domain, machines "expected")
  GET  /machine/{machineId}/attestation      measured boot · TPM PCRs · DPU attestation

The rack's machines start in lifecycle ``expected`` and become ``ready`` only
after a passing ``POST /machine/{id}/validation/run``; the rack's
``operationStatus`` follows (Initializing → Ready | Error).
Upstream (message shapes and flow):
  NVIDIA infra-controller rest-api/openapi/spec.yaml — paths
    '/v2/org/{org}/nico/expected-rack', '/rack/{id}/bringup'; schemas
    ExpectedRackCreateRequest, ExpectedRack, BringUpRackRequest/Response, NICoAPIError
  infra-controller crates/rpc/proto/measured_boot.proto — MeasurementReportPb
    {report_id, machine_id, values[{pcr_register, sha_any}], ts},
    MeasurementMachineStatePb (Discovered|PendingBundle|Measured|MeasuringFailed)
  Local copies: neocloud/ref/upstream/nico (commit not recorded in the pack yet).
  The REST spec has no per-machine attestation GET; that path is the v2
  contract (F25) and its body carries the proto report in camelCase.
Emulator hook: ``PUT /emulator/v1/rest/machine/{id}/attestation`` forces a
measured-boot / DPU attestation result.
"""
import hashlib
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Response

from .. import aiinfra
from ..store import COMPUTE_TRAYS, STORE
from . import inventory
from .common import MAX_OBJECTS, PREFIX, err, history, now, objs, page, uid

router = APIRouter(prefix=PREFIX, tags=["nico-rest-bringup"])
hooks = APIRouter(prefix="/emulator/v1/rest", tags=["nico-rest-emulator-hooks"])

PCRS = (0, 1, 2, 3, 4, 5, 7)
MEASURED_BOOT = ("Passed", "Failed", "Pending")


def _materialize(s: Dict[str, Any], rid: str, serial: str, loc: Dict[str, Any],
                 description: Optional[str] = None) -> Dict[str, Any]:
    """Create rack + trays + machines (``expected``) + NVLink domain (discovery)."""
    inv = inventory.inv()
    if rid in inv["racks"]:
        err(409, f"rack {rid} already exists")
    if any(r["serialNumber"] == serial for r in inv["racks"].values()):
        err(409, f"rack serial {serial} already registered")
    if len(inv["racks"]) >= MAX_OBJECTS:
        err(503, f"rack limit ({MAX_OBJECTS}) reached")
    sid, did = s["id"], f"nvl-{rid}"
    rack = {"id": rid, "name": rid, "manufacturer": "NVIDIA", "model": "Vera Rubin NVL72",
            "serialNumber": serial, "description": description or f"bring-up {rid}",
            "operationStatus": "Initializing", "operationStatusDetails": "bring-up",
            "nvLinkDomainIds": [did],
            "location": {"region": s.get("location", {}).get("city"),
                         "datacenter": s.get("_slug"), "room": loc.get("row"),
                         "position": loc.get("position")},
            "taskStats": {"total": 0, "pending": 0, "running": 0, "failed": 0},
            "_siteId": sid, "_suId": None, "_reports": {}, "_bringup": True, "_tasks": []}
    comps = []
    for kind, cnt, tag in (("Compute", COMPUTE_TRAYS, "tray"),
                           ("NVSwitch", inventory.NVSWITCH_TRAYS, "nvsw"),
                           ("PowerShelf", inventory.POWER_SHELVES, "ps")):
        for t in range(cnt):
            tid = f"{rid}-{tag}-{t:02d}"
            inv["trays"][tid] = {
                "id": tid, "type": kind, "name": tid, "manufacturer": "NVIDIA",
                "model": {"Compute": "VR200 compute tray", "NVSwitch": "NVLink 6 switch tray",
                          "PowerShelf": "110kW power shelf"}[kind],
                "serialNumber": f"T{inventory._h(tid) % 10**9:09d}",
                "description": f"{kind} tray {t}", "powerState": "On",
                "operationStatus": "Ready", "leakStatus": "NoLeak",
                "leakHandlingStatus": "None", "position": {"slotId": t, "trayIdx": t},
                "bmcs": [{"macAddress": "", "ipAddress": None}],
                "nvosPorts": [] if kind == "NVSwitch" else None, "rackId": rid,
                "nvLinkDomainId": did if kind != "PowerShelf" else None,
                "taskStats": {"total": 0, "pending": 0, "running": 0, "failed": 0},
                "_siteId": sid, "_fw": dict(inv["baseline"][kind]), "_reports": {}}
            comps.append({"id": tid, "type": kind, "name": tid})
            if kind == "Compute":
                mid = f"nh-{tid}"
                inv["machines"][mid] = inventory._new_machine(mid, sid, rid, tid, "expected")
    rack["components"] = comps
    inv["racks"][rid] = rack
    inv["domains"][did] = {"id": did, "rackGroupId": f"rg-{rid}", "nmxcClusterId": f"nmxc-{rid}",
                           "name": f"{rid} NVL72", "topology": "NVL72",
                           "operationStatus": "Ready", "_siteId": sid, "_rackId": rid,
                           "components": [c for c in comps if c["type"] != "PowerShelf"]}
    history(rack, "Registered", "rack registered for bring-up")
    STORE.event("info", "NICo.2.3.RackRegistered", [rid, serial])
    return rack


def _site(value: Any) -> Dict[str, Any]:
    s = inventory.site(str(value))
    if not s:
        err(404, f"site {value} not found")
    return s


@router.post("/rack", status_code=201)
def create_rack(body: Dict[str, Any] = Body(...)):
    """v2 contract alias of expected-rack + discovery."""
    name, serial = body.get("name"), body.get("serial")
    if not name or not body.get("siteId") or not serial:
        err(400, "name, siteId and serial are required")
    s = _site(body["siteId"])
    rack = _materialize(s, name, serial, body.get("location") or {}, body.get("description"))
    _expected(s, name, name, body.get("description"),
              {"chassis.serial-number": serial, "chassis.manufacturer": "NVIDIA",
               "chassis.model": "VR-NVL72",
               "location.room": (body.get("location") or {}).get("row"),
               "location.position": (body.get("location") or {}).get("position")})
    return inventory.rack_view(rack)


def _expected(s: Dict[str, Any], rack_id: str, name: Optional[str],
              description: Optional[str], labels: Dict[str, Any]) -> Dict[str, Any]:
    ts = now()
    e = {"id": uid("expected-rack", f"{s['id']}/{rack_id}"), "rackId": rack_id,
         "siteId": s["id"], "rackProfileId": "VR_NVL72_NVIDIA", "rackGroupId": None,
         "name": name or rack_id, "description": description or "",
         "labels": {k: str(v) for k, v in labels.items() if v is not None},
         "created": ts, "updated": ts}
    objs("expected-rack")[e["id"]] = e
    return e


@router.post("/expected-rack", status_code=201)
def create_expected_rack(body: Dict[str, Any] = Body(...)):
    """Upstream ExpectedRackCreateRequest {siteId, rackId, name?, description?, labels?}.
    The emulator discovers the rack at once (trays, machines ``expected``)."""
    rid = body.get("rackId")
    if not rid or not body.get("siteId"):
        err(400, "siteId and rackId are required")
    labels = body.get("labels") or {}
    if not isinstance(labels, dict):
        err(400, "labels must be an object")
    s = _site(body["siteId"])
    if any(e["rackId"] == rid and e["siteId"] == s["id"] for e in objs("expected-rack").values()):
        err(409, f"Expected Rack {rid} already exists at this Site")
    serial = labels.get("chassis.serial-number") or f"VR{inventory._h(rid) % 10**8:08d}"
    _materialize(s, rid, serial, {"row": labels.get("location.room"),
                                  "position": labels.get("location.position")},
                 body.get("description"))
    return _expected(s, rid, body.get("name"), body.get("description"), labels)


@router.get("/expected-rack")
def list_expected_racks(response: Response, siteId: Optional[str] = None,
                        pageNumber: int = 1, pageSize: int = 20):
    sid = inventory.site_id_of(siteId)
    items = [e for e in objs("expected-rack").values() if not sid or e["siteId"] == sid]
    return page(response, items, pageNumber, pageSize)


@router.get("/expected-rack/{id}")
def get_expected_rack(id: str):
    e = objs("expected-rack").get(id) or next(
        (x for x in objs("expected-rack").values() if x["rackId"] == id), None)
    if not e:
        err(404, f"Expected Rack {id} not found")
    return e


@router.post("/rack/{id}/bringup")
def bringup_rack(id: str, body: Dict[str, Any] = Body(...)):
    """Upstream BringUpRackRequest {siteId, description?} → BringUpRackResponse {taskIds}.
    Flow task: power on → firmware → NVLink domain; completes synchronously here."""
    r = inventory.inv()["racks"].get(id)
    if not r:
        err(404, f"rack {id} not found")
    if not body.get("siteId"):
        err(400, "siteId is required")
    if inventory.site_id_of(str(body["siteId"])) != r["_siteId"]:
        err(400, "rack does not belong to siteId")
    tid = uid("task")
    r.setdefault("_tasks", []).append({"id": tid, "type": "BringUp", "status": "Completed",
                                       "description": body.get("description"),
                                       "created": now()})
    r["taskStats"]["total"] += 1
    STORE.event("info", "NICo.2.3.RackBringUp", [id, tid])
    return {"taskIds": [tid]}


def refresh_rack(rack_id: Optional[str]) -> None:
    """Bring-up rack status follows its machines (called after validation runs)."""
    r = inventory.inv()["racks"].get(rack_id or "")
    if not r or not r.get("_bringup"):
        return
    ms = [m for m in inventory.inv()["machines"].values() if m.get("_rackId") == r["id"]]
    lcs = {inventory.lifecycle(m) for m in ms}
    st = "Error" if "failed" in lcs else ("Ready" if lcs <= {"ready", "assigned"} else
                                          "Initializing")
    if st != r["operationStatus"]:
        r["operationStatus"] = st
        r["operationStatusDetails"] = None if st == "Ready" else "bring-up"
        history(r, st, f"rack {st}")


def _pcr(mid: str, n: int, bad: bool) -> str:
    return hashlib.sha256(f"{mid}:pcr{n}:{'x' if bad else 'golden'}".encode()).hexdigest()


def _dpu_attested(m: Dict[str, Any]) -> Dict[str, Any]:
    dpu_id = (m.get("associatedDpuMachineIds") or [None])[0]
    forced = m.get("_attest", {}).get("dpuAttested")
    if forced is not None:
        return {"dpuMachineId": dpu_id, "attested": bool(forced), "source": "nico"}
    if not dpu_id:
        return {"dpuMachineId": None, "attested": False, "source": "nico"}
    try:
        d = aiinfra.get_dpu(dpu_id) or {}
    except aiinfra.AIInfraError:
        d = {}
    state = d.get("attestation_state")
    if state:
        return {"dpuMachineId": dpu_id, "attested": state == "VALID",
                "state": state, "source": "ai-infra"}
    return {"dpuMachineId": dpu_id, "attested": True, "source": "nico"}


@router.get("/machine/{machineId}/attestation")
def machine_attestation(machineId: str):
    m = inventory.machine(machineId)
    if not m:
        err(404, f"machine {machineId} not found")
    forced = m.get("_attest", {})
    mb = forced.get("measuredBoot") or "Passed"
    bad = mb == "Failed"
    dpu = _dpu_attested(m)
    ts = now()
    report_id = uid("measurement-report", f"{m['id']}/{'bad' if bad else 'golden'}")
    state = {"Passed": "Measured", "Failed": "MeasuringFailed"}.get(mb, "PendingBundle")
    return {"machineId": m["id"], "measuredBoot": mb, "state": state,
            "report": {"reportId": report_id, "machineId": m["id"], "ts": ts,
                       "values": [{"valueId": uid("measurement-value", f"{report_id}/{n}"),
                                   "reportId": report_id, "pcrRegister": n,
                                   "shaAny": _pcr(m["id"], n, bad), "ts": ts}
                                  for n in PCRS]},
            "attested": mb == "Passed" and dpu["attested"],
            "tpm": {"version": "2.0", "ekCertificate": "verified", "bank": "sha256",
                    "pcrs": {str(n): _pcr(m["id"], n, bad) for n in PCRS}},
            "dpu": dpu, "policy": "firmware-baseline + secure-boot chain (SPDM)",
            "verifiedAt": ts}


@hooks.put("/machine/{machineId}/attestation")
def force_attestation(machineId: str, body: Dict[str, Any] = Body(...)):
    m = inventory.machine(machineId)
    if not m:
        err(404, f"machine {machineId} not found")
    mb = body.get("measuredBoot")
    if mb is not None and mb not in MEASURED_BOOT:
        err(400, f"measuredBoot must be one of {list(MEASURED_BOOT)}")
    m["_attest"] = {k: body[k] for k in ("measuredBoot", "dpuAttested") if k in body}
    return machine_attestation(machineId)
