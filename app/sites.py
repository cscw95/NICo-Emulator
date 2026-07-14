"""Site Controllers — per-site NICo control-plane instances.

NICo is a site-local, zero-trust control plane (design §2/§3.2 — NICo Core +
REST + Temporal + site-agent + site-manager, Rack/Machine controllers,
provisioning services, security). Each AI-factory site runs its own NICo
instance managing that site's racks. This module derives the per-site
instance view (services, managed fleet, workflows) by aggregating the
physical fleet from the AI Infra Emulator (:9100) over REST — NICo no longer
owns the twin, only the site topology metadata and the control-plane view.
"""
import time

from fastapi import APIRouter, HTTPException

from .store import STORE, CLUSTER, GPU_PER_TRAY
from . import aiinfra

router = APIRouter(prefix="/emulator/v1/sites", tags=["site-controllers"])


def _site_meta(site_id: str):
    for s in CLUSTER:
        if s["id"] == site_id:
            return s
    return None


def _site_of(name_or_id: str) -> str:
    for s in CLUSTER:
        if s["id"] == name_or_id or s["name"] == name_or_id:
            return s["id"]
    return name_or_id


def _empty_fleet(site_id: str, meta) -> dict:
    return {"site_id": site_id, "site": meta["name"] if meta else site_id,
            "region": meta["region"] if meta else "",
            "sus": [su for su, _ in (meta["sus"] if meta else [])],
            "racks": 0, "trays": 0, "gpus": 0, "dpus": 0,
            "hosts_by_lifecycle": {}, "power": {"On": 0, "PoweringOn": 0, "Off": 0},
            "tenants": [], "dpu_degraded": 0, "dpu_warning": 0,
            "ai_infra": False}


def _fleet(site_id: str) -> dict:
    """Aggregate the physical fleet a site's NICo manages, from AI Infra."""
    meta = _site_meta(site_id)
    site_name = meta["name"] if meta else site_id
    try:
        racks = aiinfra.list_racks(site=site_id, limit=500).get("racks", [])
    except aiinfra.AIInfraError:
        return _empty_fleet(site_id, meta)

    trays = gpus = dpus = 0
    power = {"On": 0, "PoweringOn": 0, "Off": 0}
    life = {"ready": 0, "attention": 0, "degraded": 0}
    tenants = set()
    degraded = warn = 0
    for r in racks:
        trays += r.get("trays", 0)
        gpus += r.get("gpus", 0)
        dpus += r.get("dpus", 0)
        for k, v in (r.get("power") or {}).items():
            power[k] = power.get(k, 0) + v
        rh = r.get("health") or {}
        degraded += rh.get("critical", 0)
        warn += rh.get("warning", 0)
        life[r.get("state", "ready")] = life.get(r.get("state", "ready"), 0) + 1
        tenants.update(r.get("tenants") or [])
    return {"site_id": site_id, "site": site_name,
            "region": meta["region"] if meta else "",
            "sus": [su for su, _ in (meta["sus"] if meta else [])],
            "racks": len(racks), "trays": trays, "gpus": gpus, "dpus": dpus,
            "hosts_by_lifecycle": life, "power": power,
            "tenants": sorted(tenants), "dpu_degraded": degraded,
            "dpu_warning": warn, "ai_infra": True}


def _leases_for_site(site_id: str) -> int:
    try:
        leases = aiinfra.list_leases()
    except aiinfra.AIInfraError:
        return 0
    from .store import site_of_tray
    return sum(1 for l in leases if site_of_tray(l.get("tray_id", ""))[0] == site_id)


def _services(site_id: str, f: dict, leases: int):
    """NICo instance service inventory (design §3.1/§3.2) with derived status."""
    provisioning = f["hosts_by_lifecycle"].get("attention", 0) or leases
    health = ("critical" if f["dpu_degraded"] else
              "warning" if f["dpu_warning"] else "ok")
    conn = "ok" if f.get("ai_infra") else "warning"
    return [
        {"name": "NICo API Service", "component": "carbide · gRPC/mTLS",
         "status": "ok", "detail": "state-machine single writer · PostgreSQL"},
        {"name": "NICo REST API", "component": "OpenAPI northbound",
         "status": "ok", "detail": "NeoCloud OS 연동 · /nico-bridge"},
        {"name": "AI Infra Link", "component": "physical twin (:9100)",
         "status": conn, "detail": (f"{f['trays']} host(s) reconciled via REST"
                                    if f.get("ai_infra") else "unreachable")},
        {"name": "Site Workflow", "component": "Temporal",
         "status": "ok", "detail": f"{provisioning} active provisioning workflow(s)"},
        {"name": "Site Agent", "component": "site-local executor",
         "status": "ok", "detail": "drives local NICo Core"},
        {"name": "Rack Manager", "component": "rack-controller",
         "status": "ok", "detail": f"{f['racks']} rack(s) managed"},
        {"name": "Machine Controller", "component": "machine-controller",
         "status": "ok", "detail": f"{f['trays']} host(s) · lifecycle reconcile"},
        {"name": "BMC / Redfish Gateway", "component": "bmc-proxy · nv_redfish",
         "status": "ok", "detail": f"{f['power']['On']} on · {f['power']['Off']} off"},
        {"name": "DHCP Server", "component": "dhcp-server",
         "status": "ok", "detail": f"{leases} active lease(s)"},
        {"name": "PXE / iPXE", "component": "pxe · ipxe-renderer",
         "status": "ok", "detail": "bare-metal OS provisioning"},
        {"name": "DNS", "component": "authoritative + recursive",
         "status": "ok", "detail": "provisioning network service"},
        {"name": "InfiniBand Fabric (UFM)", "component": "ib-partition-controller",
         "status": "ok", "detail": "P_Key partition · tenant isolation"},
        {"name": "NVLink / NVSwitch", "component": "nvlink-manager",
         "status": "ok", "detail": "NVLink domain management"},
        {"name": "Power Shelf", "component": "power-shelf-controller",
         "status": "ok", "detail": "rack power resource"},
        {"name": "DPU / DPA / DPF", "component": "dpa-manager · dpf",
         "status": health,
         "detail": (f"{f['dpu_degraded']} degraded · {f['dpu_warning']} warning"
                    if (f["dpu_degraded"] or f["dpu_warning"])
                    else f"{f['dpus']} DPU · zero-trust isolation")},
        {"name": "Health / Remediation", "component": "health · dpu-remediation",
         "status": health, "detail": "sensor + DCGM adapter"},
        {"name": "Measured Boot / SPDM", "component": "spdm-controller",
         "status": "ok", "detail": "device attestation"},
    ]


def _instance(site_id: str):
    f = _fleet(site_id)
    leases = _leases_for_site(site_id)
    svcs = _services(site_id, f, leases)
    status = ("critical" if any(s["status"] == "critical" for s in svcs) else
              "degraded" if any(s["status"] == "warning" for s in svcs) else "healthy")
    return {
        "site_id": site_id, "site": f["site"], "region": f["region"],
        "nico_instance": f"nico-{site_id}", "version": "0.1.0",
        "ha_nodes": 3, "leader": f"nico-{site_id}-0",
        "deployment": "NICo Core + REST + Temporal + Keycloak + site-agent",
        "status": status,
        "services": svcs,
        "service_ok": sum(1 for s in svcs if s["status"] == "ok"),
        "service_total": len(svcs),
        "fleet": f,
    }


@router.get("")
def list_sites():
    """Per-site NICo control-plane instances (cross-site independent)."""
    with STORE.lock:
        return {"sites": [_instance(s["id"]) for s in CLUSTER],
                "cross_site_note": "independent NICo instances — no cross-site "
                "IB/NVLink; each site is its own control-plane + fabric domain"}


@router.get("/{site_id}")
def get_site(site_id: str):
    with STORE.lock:
        sid = _site_of(site_id)
        meta = _site_meta(sid)
        if not meta:
            raise HTTPException(404, f"site {site_id} not found")
        inst = _instance(sid)
        # per-SU / per-rack roll-up for drill-down (from AI Infra)
        try:
            racks = aiinfra.list_racks(site=sid, limit=500).get("racks", [])
        except aiinfra.AIInfraError:
            racks = []
        sus = {}
        for r in racks:
            su = sus.setdefault(r["su_id"], {"su_id": r["su_id"], "racks": 0,
                                             "gpus": 0, "tenants": set()})
            su["racks"] += 1; su["gpus"] += r.get("gpus", 0)
            su["tenants"].update(r.get("tenants") or [])
        inst["scalable_units"] = [
            {**v, "tenants": sorted(v["tenants"])}
            for v in sorted(sus.values(), key=lambda x: int(x["su_id"].split("-")[1]))]
        inst["racks"] = racks
        inst["recent_events"] = [e for e in list(STORE.events)[-40:]
                                 if inst["site"] in str(e.get("args", []))
                                 or sid in str(e.get("args", []))][-12:][::-1]
        inst["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return inst
