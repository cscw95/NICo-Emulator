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

from .store import STORE, CLUSTER, GPU_PER_TRAY, site_of_tray
from . import aiinfra
from . import bridge

router = APIRouter(prefix="/emulator/v1/sites", tags=["site-controllers"])

# host lifecycle states NICo's orchestration tracks (bridge host state machine)
HOST_STATES = ("pool_ready", "reserved", "provisioning", "provisioned",
               "allocated", "released")


def _tray_of_host(host_id: str) -> str:
    return host_id[3:] if host_id.startswith("nh-") else host_id


def _orchestration(site_id: str) -> dict:
    """NICo's own orchestration state for a site, aggregated from the NOCP
    bridge (host lifecycle state machine, jobs, tenant segments). Every host
    the bridge has touched maps to a site via host_id=nh-{tray_id}."""
    hosts_by_state = {s: 0 for s in HOST_STATES}
    managed = 0
    tenants = set()
    for hid, h in bridge._hosts.items():
        tid = h.get("tray_id") or _tray_of_host(hid)
        if site_of_tray(tid)[0] != site_id:
            continue
        managed += 1
        st = h.get("state", "pool_ready")
        hosts_by_state[st] = hosts_by_state.get(st, 0) + 1
        if h.get("tenant_id"):
            tenants.add(h["tenant_id"])

    site_jobs = [j for j in bridge._jobs.values()
                 if site_of_tray(_tray_of_host(j.get("host_id", "")))[0] == site_id]
    active_jobs = sum(1 for j in site_jobs if j.get("state") == "running")
    failed_jobs = sum(1 for j in site_jobs
                      if j.get("state") in ("failed", "error", "aborted"))
    recent_jobs = [{"job_id": j["job_id"], "op": j["op"], "host_id": j["host_id"],
                    "state": j["state"], "detail": j.get("detail", "")}
                   for j in site_jobs[-8:]][::-1]

    segments = []
    for s in bridge._segments.values():
        host_ids = s.get("host_ids", [])
        seg_site = (site_of_tray(_tray_of_host(host_ids[0]))[0]
                    if host_ids else None)
        if seg_site != site_id:
            continue
        segments.append({"segment_id": s["segment_id"],
                         "tenant_ref": s["tenant_ref"], "vrf": s["vrf"],
                         "l3vni": s["l3vni"], "host_count": len(host_ids)})
        tenants.add(s["tenant_ref"])

    # AI Infra의 실제 DPU 격리 병합 — NICo 재기동으로 브리지 인메모리가 비어도
    # 물리 트윈에 살아있는 개통(allocated) 상태를 세부 동작 상태에 반영한다.
    try:
        f = _fleet(site_id)
        allocated_live = f.get("allocated_hosts", 0)
        live_tenants = set(f.get("tenants") or [])
    except Exception:
        allocated_live, live_tenants = 0, set()
    if allocated_live > hosts_by_state.get("allocated", 0):
        hosts_by_state["allocated"] = allocated_live      # 물리 격리 기준
        managed = max(managed, allocated_live)
    tenants |= live_tenants
    # 세그먼트가 브리지에 없으면(재기동) 실 격리 테넌트로 합성 표기
    seg_tenants = {s["tenant_ref"] for s in segments}
    for t in sorted(live_tenants - seg_tenants):
        segments.append({"segment_id": f"vpc-{t}", "tenant_ref": t,
                         "vrf": f"VRF-{t}", "l3vni": None,
                         "host_count": None, "source": "ai-infra"})

    return {"hosts_by_state": hosts_by_state, "managed_hosts": managed,
            "active_jobs": active_jobs, "failed_jobs": failed_jobs,
            "recent_jobs": recent_jobs, "segments": segments,
            "tenants_served": sorted(tenants)}


def _ha(site_id: str, term: int) -> dict:
    """Deterministic 3-node Raft HA view (leader + 2 followers, 3/3 quorum).
    raft_term advances with the control-plane event log (Assumption)."""
    nodes = [
        {"name": f"nico-{site_id}-0", "role": "leader",
         "state": "healthy", "raft_term": term},
        {"name": f"nico-{site_id}-1", "role": "follower",
         "state": "healthy", "raft_term": term},
        {"name": f"nico-{site_id}-2", "role": "follower",
         "state": "healthy", "raft_term": term},
    ]
    return {"nodes": nodes, "quorum": "3/3", "leader": f"nico-{site_id}-0"}


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

    trays = gpus = dpus = allocated = 0
    power = {"On": 0, "PoweringOn": 0, "Off": 0}
    life = {"ready": 0, "attention": 0, "degraded": 0}
    tenants = set()
    degraded = warn = 0
    for r in racks:
        trays += r.get("trays", 0)
        gpus += r.get("gpus", 0)
        dpus += r.get("dpus", 0)
        rt = r.get("tenants") or []
        if rt:                                  # 테넌트 격리된 랙 = 18 host 할당
            allocated += r.get("trays", 0)
        for k, v in (r.get("power") or {}).items():
            power[k] = power.get(k, 0) + v
        rh = r.get("health") or {}
        degraded += rh.get("critical", 0)
        warn += rh.get("warning", 0)
        life[r.get("state", "ready")] = life.get(r.get("state", "ready"), 0) + 1
        tenants.update(rt)
    return {"site_id": site_id, "site": site_name,
            "region": meta["region"] if meta else "",
            "sus": [su for su, _ in (meta["sus"] if meta else [])],
            "racks": len(racks), "trays": trays, "gpus": gpus, "dpus": dpus,
            "allocated_hosts": allocated,
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


def _services(site_id: str, f: dict, leases: int, orch: dict):
    """NICo instance service inventory (design §3.1/§3.2) with derived status.
    Workflow / Machine Controller / API details are wired to NICo's live
    orchestration state (bridge host lifecycle, jobs, segments)."""
    provisioning = f["hosts_by_lifecycle"].get("attention", 0) or leases
    health = ("critical" if f["dpu_degraded"] else
              "warning" if f["dpu_warning"] else "ok")
    conn = "ok" if f.get("ai_infra") else "warning"
    hs = orch["hosts_by_state"]
    managed = orch["managed_hosts"]
    wf_status = "warning" if orch["failed_jobs"] else "ok"
    wf_detail = (f"{orch['active_jobs']} running · {orch['failed_jobs']} failed · "
                 f"{len(orch['recent_jobs'])} recent job(s)"
                 if managed or orch["recent_jobs"]
                 else f"{provisioning} provisioning workflow(s) · idle")
    mc_detail = (f"{managed} orchestrated · alloc {hs['allocated']} · "
                 f"prov {hs['provisioned']} · resv {hs['reserved']} · "
                 f"pool {hs['pool_ready']}"
                 if managed else f"{f['trays']} host(s) · lifecycle reconcile · idle")
    api_detail = (f"single writer · PostgreSQL · {managed} host(s) orchestrated · "
                  f"{len(orch['segments'])} segment(s)")
    return [
        {"name": "NICo API Service", "component": "carbide · gRPC/mTLS",
         "status": "ok", "detail": api_detail},
        {"name": "NICo REST API", "component": "OpenAPI northbound",
         "status": "ok", "detail": "NeoCloud OS 연동 · /nico-bridge"},
        {"name": "AI Infra Link", "component": "physical twin (:9100)",
         "status": conn, "detail": (f"{f['trays']} host(s) reconciled via REST"
                                    if f.get("ai_infra") else "unreachable")},
        {"name": "Site Workflow", "component": "Temporal",
         "status": wf_status, "detail": wf_detail},
        {"name": "Site Agent", "component": "site-local executor",
         "status": "ok", "detail": "drives local NICo Core"},
        {"name": "Rack Manager", "component": "rack-controller",
         "status": "ok", "detail": f"{f['racks']} rack(s) managed"},
        {"name": "Machine Controller", "component": "machine-controller",
         "status": "ok", "detail": mc_detail},
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
    orch = _orchestration(site_id)
    svcs = _services(site_id, f, leases, orch)
    status = ("critical" if any(s["status"] == "critical" for s in svcs) else
              "degraded" if any(s["status"] == "warning" for s in svcs) else "healthy")
    term = 1 + len(STORE.events) // 8   # deterministic Raft term (event-driven)
    return {
        "site_id": site_id, "site": f["site"], "region": f["region"],
        "nico_instance": f"nico-{site_id}", "version": "0.1.0",
        "ha_nodes": 3, "leader": f"nico-{site_id}-0",
        "ha": _ha(site_id, term),
        "deployment": "NICo Core + REST + Temporal + Keycloak + site-agent",
        "status": status,
        "services": svcs,
        "service_ok": sum(1 for s in svcs if s["status"] == "ok"),
        "service_total": len(svcs),
        "orchestration": orch,
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
