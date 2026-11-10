"""NICo REST inventory — sites, racks, trays, NVLink domains, machines.

Seeded lazily from the static site topology (``store.CLUSTER``): 2 sites,
140 VR NVL72 racks, 18 compute trays + 9 NVSwitch trays + 8 power shelves per
rack, one NVLink domain per rack, one Machine per compute tray (2,520).

Machine ids equal the ``/nico-bridge`` host ids (``nh-{trayId}``) so both
surfaces share one state: the bridge host record (``bridge._hosts``) drives
assignment, while this module adds the REST-only Day0→2 lifecycle
(expected → discovered → validating → ready → assigned → sanitizing → ready |
failed), health reports, validation runs and firmware.
"""
import os
import time
import zlib
from typing import Any, Dict, List, Optional

from ..store import CLUSTER, COMPUTE_TRAYS, GPU_PER_TRAY
from .. import bridge
from .common import PROVIDER_ID, history, now, uid

NVSWITCH_TRAYS = 9
POWER_SHELVES = 8

# lifecycleState -> spec MachineStatus
LIFECYCLE_STATUS = {
    "expected": "Initializing", "discovered": "Initializing",
    "validating": "Initializing", "ready": "Ready", "assigned": "InUse",
    "sanitizing": "Reset", "failed": "Error",
    "decommissioned": "Decommissioned",
}
LIFECYCLE_NEXT = {"expected": "discovered", "discovered": "validating"}
BRIDGE_ASSIGNED = ("reserved", "provisioning", "provisioned", "allocated")

# fleet firmware baseline per tray type (component -> version)
DEFAULT_BASELINE = {
    "Compute": {"BMC": "25.10-1", "BIOS": "2.3.1", "GPU": "97.10.42", "DPU": "45.2.1000"},
    "NVSwitch": {"BMC": "25.10-1", "NVOS": "25.02.2210"},
    "PowerShelf": {"PSU": "3.4.0"},
}
DRIFTED = {"Compute": {"GPU": "97.08.31"}, "NVSwitch": {"NVOS": "25.01.1800"},
           "PowerShelf": {"PSU": "3.2.2"}}

INV: Dict[str, Any] = {}


def _h(key: str) -> int:
    return zlib.crc32(key.encode())


def sanitize_seconds() -> float:
    return float(os.environ.get("NICO_SANITIZE_SECONDS", "2"))


def reset() -> None:
    INV.clear()


def _seed() -> None:
    sites, racks, trays, machines, domains = {}, {}, {}, {}, {}
    ts = now()
    for site in CLUSTER:
        sid = uid("site", site["id"])
        s = {"id": sid, "name": site["name"], "description": f"{site['region']} AI factory",
             "org": "neocloud", "infrastructureProviderId": PROVIDER_ID,
             "siteControllerVersion": "2.3.0", "siteAgentVersion": "2.3.0",
             "isSerialConsoleEnabled": False, "isSerialConsoleSSHKeysEnabled": False,
             "isOnline": True, "location": {"city": site["region"], "country": "KR"},
             "contact": {"email": "noc@neocloud.example"},
             "capabilities": {"nativeNetworking": True, "networkSecurityGroup": True,
                              "nvLinkPartition": True, "flow": True, "spectrumX": True},
             "created": ts, "updated": ts, "statusHistory": [], "_slug": site["id"]}
        history(s, "Registered", "Site registered")
        sites[sid] = s
        n = 0
        for su, count in site["sus"]:
            for r in range(count):
                rid = f"{su}-rack-{r:02d}"
                did = f"nvl-{rid}"
                domains[did] = {"id": did, "rackGroupId": f"rg-{su}", "nmxcClusterId": f"nmxc-{rid}",
                                "name": f"{rid} NVL72", "topology": "NVL72",
                                "operationStatus": "Ready", "_siteId": sid, "_rackId": rid,
                                "components": []}
                racks[rid] = {"id": rid, "name": rid, "manufacturer": "NVIDIA",
                              "model": "Vera Rubin NVL72", "serialNumber": f"VR{_h(rid) % 10**8:08d}",
                              "description": f"{site['name']} {su} rack {r:02d}",
                              "operationStatus": "Ready", "operationStatusDetails": None,
                              "nvLinkDomainIds": [did],
                              "location": {"region": site["region"], "datacenter": site["id"],
                                           "room": su, "position": f"{su}-{r:02d}"},
                              "taskStats": {"total": 0, "pending": 0, "running": 0, "failed": 0},
                              "_siteId": sid, "_suId": su, "_reports": {}}
                comps = []
                for kind, cnt, tag in (("Compute", COMPUTE_TRAYS, "tray"),
                                       ("NVSwitch", NVSWITCH_TRAYS, "nvsw"),
                                       ("PowerShelf", POWER_SHELVES, "ps")):
                    for t in range(cnt):
                        tid = f"{rid}-{tag}-{t:02d}"
                        fw = dict(DEFAULT_BASELINE[kind])
                        n += 1
                        if n % 97 == 0:                 # a few trays off-baseline
                            fw.update(DRIFTED[kind])
                        trays[tid] = {
                            "id": tid, "type": kind, "name": tid, "manufacturer": "NVIDIA",
                            "model": {"Compute": "VR200 compute tray", "NVSwitch": "NVLink 6 switch tray",
                                      "PowerShelf": "33kW power shelf"}[kind],
                            "serialNumber": f"T{_h(tid) % 10**9:09d}",
                            "description": f"{kind} tray {t}", "powerState": "On",
                            "operationStatus": "Ready", "leakStatus": "NoLeak",
                            "leakHandlingStatus": "None",
                            "position": {"slotId": t, "trayIdx": t},
                            "bmcs": [{"macAddress": "", "ipAddress": None}],
                            "nvosPorts": [] if kind == "NVSwitch" else None,
                            "rackId": rid,
                            "nvLinkDomainId": did if kind != "PowerShelf" else None,
                            "taskStats": {"total": 0, "pending": 0, "running": 0, "failed": 0},
                            "_siteId": sid, "_fw": fw, "_reports": {}}
                        comps.append({"id": tid, "type": kind, "name": tid})
                        if kind == "Compute":
                            mid = f"nh-{tid}"
                            machines[mid] = _new_machine(mid, sid, rid, tid, "ready")
                racks[rid]["components"] = comps
                domains[did]["components"] = [c for c in comps if c["type"] != "PowerShelf"]
    INV.update(sites=sites, racks=racks, trays=trays, machines=machines, domains=domains,
               baseline={k: dict(v) for k, v in DEFAULT_BASELINE.items()})


def _new_machine(mid: str, site_id: str, rack_id: Optional[str], tray_id: Optional[str],
                 lifecycle: str, sku: str = "vr-nvl72") -> Dict[str, Any]:
    ts = now()
    m = {"id": mid, "infrastructureProviderId": PROVIDER_ID, "siteId": site_id,
         "instanceTypeId": uid("instance-type", f"{site_id}/{sku}"),
         "controllerMachineId": mid, "controllerMachineType": sku,
         "hwSkuDeviceType": "gpu" if sku == "vr-nvl72" else "cpu",
         "vendor": "NVIDIA" if sku == "vr-nvl72" else "AMD",
         "productName": "VR200 compute tray" if sku == "vr-nvl72" else "EPYC node",
         "serialNumber": f"M{_h(mid) % 10**9:09d}",
         "machineCapabilities": ([{"type": "GPU", "name": "NVIDIA Rubin", "count": GPU_PER_TRAY}]
                                 if sku == "vr-nvl72" else [{"type": "CPU", "name": "AMD EPYC", "count": 2}]),
         "machineInterfaces": [], "associatedDpuMachineIds": [f"{tray_id}-dpu-0"] if tray_id else [],
         "placementInRack": {"rackId": rack_id, "trayId": tray_id},
         "maintenanceMessage": None, "labels": {}, "created": ts, "updated": ts,
         "statusHistory": [], "_lifecycle": lifecycle, "_sanitize_until": 0.0,
         "_reports": {}, "_runs": [], "_results": [], "_trayId": tray_id, "_rackId": rack_id}
    history(m, LIFECYCLE_STATUS[lifecycle], f"lifecycle {lifecycle}")
    return m


def inv() -> Dict[str, Any]:
    if not INV:
        _seed()
    return INV


# ── sites ─────────────────────────────────────────────────────────────
def site(site_id: str) -> Optional[Dict[str, Any]]:
    s = inv()["sites"]
    if site_id in s:
        return s[site_id]
    for v in s.values():
        if v.get("_slug") == site_id or v["name"] == site_id:
            return v
    return None


def site_id_of(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    s = site(value)
    return s["id"] if s else value


# ── machines ──────────────────────────────────────────────────────────
def lifecycle(m: Dict[str, Any]) -> str:
    """Effective lifecycle: REST state overlaid with the bridge host state."""
    lc = m["_lifecycle"]
    if lc == "sanitizing" and time.time() >= m["_sanitize_until"]:
        m["_lifecycle"] = lc = "ready"
        history(m, "Ready", "sanitize complete")
    if lc in ("ready", "assigned"):
        h = bridge._hosts.get(m["id"])
        if h is not None:
            return "assigned" if h.get("state") in BRIDGE_ASSIGNED or h.get("instance_id") else "ready"
    return lc


def machine_health(m: Dict[str, Any]) -> Dict[str, Any]:
    alerts, successes = [], [{"id": "BmcReachable", "target": None},
                             {"id": "PowerSupply", "target": None}]
    reports = list(m["_reports"].values())
    tray = inv()["trays"].get(m.get("_trayId") or "")
    if tray:
        reports += list(tray["_reports"].values())
        if tray["leakStatus"] == "Leaking":
            alerts.append({"id": "Leak", "target": tray["id"], "inAlertSince": None,
                           "message": "Liquid cooling leak detected",
                           "tenantMessage": None,
                           "classifications": ["PreventAllocations"]})
    for r in reports:
        alerts += r.get("alerts") or []
        successes += r.get("successes") or []
    return {"source": "aggregate-host-health", "observedAt": now(),
            "successes": successes, "alerts": alerts}


def is_healthy(m: Dict[str, Any]) -> bool:
    return not any("PreventAllocations" in (a.get("classifications") or [])
                   for a in machine_health(m)["alerts"])


def machine_view(m: Dict[str, Any]) -> Dict[str, Any]:
    lc = lifecycle(m)
    h = bridge._hosts.get(m["id"]) or {}
    status = LIFECYCLE_STATUS[lc]
    if h.get("cordoned") and status in ("Ready", "InUse"):
        status = "Maintenance"
    v = {k: val for k, val in m.items() if not k.startswith("_")}
    v.update(status=status, lifecycleState=lc, health=machine_health(m),
             instanceId=h.get("instance_id") if lc == "assigned" else None,
             tenantId=(h.get("tenant_id") or h.get("tenant_ref")) if lc == "assigned" else None,
             isUsableByTenant=lc == "ready" and is_healthy(m),
             metadata={"rackId": m.get("_rackId"), "trayId": m.get("_trayId")})
    return v


def machine(mid: str) -> Optional[Dict[str, Any]]:
    ms = inv()["machines"]
    if mid in ms:
        return ms[mid]
    if f"nh-{mid}" in ms:                # tray id accepted as alias
        return ms[f"nh-{mid}"]
    if mid in bridge._hosts:            # bridge-only host (e.g. cpu-node-XX)
        h = bridge._hosts[mid]
        sid = next(iter(inv()["sites"]))
        ms[mid] = _new_machine(mid, sid, None, None, "ready", h.get("sku", "cpu-epyc"))
        return ms[mid]
    return None


def set_lifecycle(m: Dict[str, Any], lc: str, message: str = "") -> None:
    m["_lifecycle"] = lc
    if lc == "sanitizing":
        m["_sanitize_until"] = time.time() + sanitize_seconds()
    history(m, LIFECYCLE_STATUS[lc], message or f"lifecycle {lc}")


# ── trays / racks ─────────────────────────────────────────────────────
def tray_health(t: Dict[str, Any]) -> Dict[str, Any]:
    alerts = []
    for r in t["_reports"].values():
        alerts += r.get("alerts") or []
    if t["leakStatus"] == "Leaking":
        alerts.append({"id": "Leak", "target": t["id"], "message": "Leak detected",
                       "classifications": ["PreventAllocations"]})
    return {"source": "aggregate-tray-health", "observedAt": now(),
            "status": "Critical" if alerts else "OK", "alerts": alerts}


def tray_view(t: Dict[str, Any]) -> Dict[str, Any]:
    v = {k: val for k, val in t.items() if not k.startswith("_")}
    v["firmwareVersion"] = "/".join(f"{k}={ver}" for k, ver in t["_fw"].items())
    v["firmware"] = dict(t["_fw"])
    v["health"] = tray_health(t)
    v["siteId"] = t["_siteId"]
    return v


def rack_trays(rack_id: str) -> List[Dict[str, Any]]:
    r = inv()["racks"][rack_id]
    trays = inv()["trays"]
    return [trays[c["id"]] for c in r["components"]]


def rack_view(r: Dict[str, Any], deep: bool = True) -> Dict[str, Any]:
    trays = rack_trays(r["id"])
    leaking = [t["id"] for t in trays if t["leakStatus"] == "Leaking"]
    alerts = []
    for rep in r["_reports"].values():
        alerts += rep.get("alerts") or []
    unhealthy = [t["id"] for t in trays if tray_health(t)["alerts"]]
    counts: Dict[str, Dict[str, int]] = {}
    for t in trays:
        c = counts.setdefault(t["type"], {"total": 0, "ready": 0, "error": 0})
        c["total"] += 1
        c["error" if t["operationStatus"] == "Error" or tray_health(t)["alerts"] else "ready"] += 1
    v = {k: val for k, val in r.items() if not k.startswith("_")}
    v.update(siteId=r["_siteId"],
             operationStatus="Error" if (alerts or leaking) else r["operationStatus"],
             health={"status": "Critical" if (alerts or leaking) else ("Warning" if unhealthy else "OK"),
                     "alerts": alerts, "unhealthyTrays": unhealthy,
                     "leakStatus": "Leaking" if leaking else "NoLeak", "leakingTrays": leaking,
                     "trayStatusCounts": counts, "observedAt": now()})
    if not deep:
        v.pop("components", None)
    return v


def drift(site_id: Optional[str] = None, rack_id: Optional[str] = None) -> List[Dict[str, Any]]:
    base = inv()["baseline"]
    out = []
    for t in inv()["trays"].values():
        if site_id and t["_siteId"] != site_id:
            continue
        if rack_id and t["rackId"] != rack_id:
            continue
        diffs = [{"component": c, "expected": want, "actual": t["_fw"].get(c)}
                 for c, want in base.get(t["type"], {}).items() if t["_fw"].get(c) != want]
        if diffs:
            out.append({"trayId": t["id"], "type": t["type"], "rackId": t["rackId"],
                        "siteId": t["_siteId"], "diffs": diffs})
    return out
