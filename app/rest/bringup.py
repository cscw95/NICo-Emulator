"""NICo REST bring-up surface (F23/F25):

  POST /rack {name, siteId, serial}          register a new rack (18 compute + 9
                                             NVSwitch trays + 4 power shelves, one
                                             NVLink domain, machines "expected")
  GET  /machine/{machineId}/attestation      measured boot · TPM PCRs · DPU attestation

The rack's machines start in lifecycle ``expected`` and become ``ready`` only
after a passing ``POST /machine/{id}/validation/run``; the rack's
``operationStatus`` follows (Initializing → Ready | Error).
Emulator hook: ``PUT /emulator/v1/rest/machine/{id}/attestation`` forces a
measured-boot / DPU attestation result.
"""
import hashlib
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body

from .. import aiinfra
from ..store import COMPUTE_TRAYS, STORE
from . import inventory
from .common import MAX_OBJECTS, PREFIX, err, history, now

router = APIRouter(prefix=PREFIX, tags=["nico-rest-bringup"])
hooks = APIRouter(prefix="/emulator/v1/rest", tags=["nico-rest-emulator-hooks"])

PCRS = (0, 1, 2, 3, 4, 5, 7)
MEASURED_BOOT = ("Passed", "Failed", "Pending")


@router.post("/rack", status_code=201)
def create_rack(body: Dict[str, Any] = Body(...)):
    name, serial = body.get("name"), body.get("serial")
    if not name or not body.get("siteId") or not serial:
        err(400, "name, siteId and serial are required")
    s = inventory.site(str(body["siteId"]))
    if not s:
        err(404, f"site {body['siteId']} not found")
    inv = inventory.inv()
    if name in inv["racks"]:
        err(409, f"rack {name} already exists")
    if any(r["serialNumber"] == serial for r in inv["racks"].values()):
        err(409, f"rack serial {serial} already registered")
    if len(inv["racks"]) >= MAX_OBJECTS:
        err(503, f"rack limit ({MAX_OBJECTS}) reached")
    sid, rid, did = s["id"], name, f"nvl-{name}"
    loc = body.get("location") or {}
    rack = {"id": rid, "name": rid, "manufacturer": "NVIDIA", "model": "Vera Rubin NVL72",
            "serialNumber": serial, "description": body.get("description") or f"bring-up {rid}",
            "operationStatus": "Initializing", "operationStatusDetails": "bring-up",
            "nvLinkDomainIds": [did],
            "location": {"region": s.get("location", {}).get("city"),
                         "datacenter": s.get("_slug"), "room": loc.get("row"),
                         "position": loc.get("position")},
            "taskStats": {"total": 0, "pending": 0, "running": 0, "failed": 0},
            "_siteId": sid, "_suId": None, "_reports": {}, "_bringup": True}
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
    return inventory.rack_view(rack)


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
    return {"machineId": m["id"], "measuredBoot": mb,
            "attested": mb == "Passed" and dpu["attested"],
            "tpm": {"version": "2.0", "ekCertificate": "verified", "bank": "sha256",
                    "pcrs": {str(n): _pcr(m["id"], n, bad) for n in PCRS}},
            "dpu": dpu, "policy": "firmware-baseline + secure-boot chain (SPDM)",
            "verifiedAt": now()}


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
