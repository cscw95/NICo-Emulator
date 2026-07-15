"""NeoCloud OS (NOCP) integration bridge.

Exposes the exact REST contract that nocp's NicoHttpAdapter speaks
(/hosts, /instances, /jobs, /segments with NicoHost/NicoJob/NicoSegment
shapes). NICo owns the *orchestration* lifecycle (host state machine, jobs,
segments) in memory; every *physical* effect is delegated to the AI Infra
Emulator (:9100) via ``app.aiinfra`` (Redfish power, PXE/DHCP provisioning,
DPU tenant-isolation attachments).

    NOCP_NICO_URL=http://127.0.0.1:9000/nico-bridge  ./run.sh

Lenient: accepts any host_id nocp sends (auto-registers on first touch). When
the id maps onto a fleet tray (nh-{tray_id}), it drives the real AI Infra
physical state; otherwise it stays a pure control-plane record.
"""
import itertools
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .store import STORE, _iso, site_name_of_tray
from . import aiinfra

router = APIRouter(prefix="/nico-bridge", tags=["nocp-bridge"])

# NICo-owned orchestration state (host_id -> record, job_id -> record)
_hosts = {}
_jobs = {}
_segments = {}
_job_seq = itertools.count(1)

SANITIZE_STEPS = ["nvme_secure_erase", "gpu_memory_wipe", "system_memory_wipe",
                  "tpm_reset", "re_attestation", "firmware_revalidation",
                  "network_state_clear"]


def reset_bridge():
    """Clear bridge-side lifecycle state (called by the emulator /reset)."""
    _hosts.clear(); _jobs.clear(); _segments.clear()


class _ProvisionBody(BaseModel):
    image_ref: str = ""


class _InstanceBody(BaseModel):
    host_id: str
    tenant_ref: str


class _CordonBody(BaseModel):
    reason: str = ""


# ── host_id <-> tray mapping ───────────────────────────────────────────
def _tray_id(host_id: str) -> str:
    return host_id[3:] if host_id.startswith("nh-") else host_id


def _dpu_id(host_id: str) -> str:
    """Fleet DPU id for a host — one BlueField DPU per compute tray."""
    return f"{_tray_id(host_id)}-dpu-0"


def _tenant_net(tenant_ref: str, dpu_id: str) -> dict:
    return {
        "network_id": f"net-{tenant_ref}-{dpu_id}",
        "tenant_id": tenant_ref, "network_type": "vxlan",
        "vni": 10000 + (hash(tenant_ref) % 6000),
        "vrf": tenant_ref, "subnet": "10.200.0.0/16",
    }


def _default_host(host_id: str) -> dict:
    tid = _tray_id(host_id)
    # CPU 풀 노드(nh-cpu-node-XX) — Managed K8s CP 등 범용 노드. 랙 트윈
    # 트레이가 아니므로 sku를 구분한다 (물리 효과는 best-effort skip).
    sku = "cpu-epyc" if tid.startswith("cpu-node-") else "vr-nvl72"
    return {"host_id": host_id, "tray_id": tid, "sku": sku,
            "site": site_name_of_tray(tid) or ("공용 풀" if sku == "cpu-epyc"
                                               else "STT 가산"),
            "state": "pool_ready",
            "firmware_ok": True, "attested": True, "cordoned": False,
            "instance_id": None}


def _host(host_id: str, create=True) -> dict:
    h = _hosts.get(host_id)
    if h is None and create:
        h = _default_host(host_id)
        _hosts[host_id] = h
    if h is None:
        raise HTTPException(404, f"host {host_id} not found")
    return h


def _view(h: dict) -> dict:
    v = {k: val for k, val in h.items() if not k.startswith("_")}
    # NicoHost 계약 호환: 내부 tenant_id → tenant_ref 로 노출
    if "tenant_id" in v:
        v.setdefault("tenant_ref", v["tenant_id"])
    v.setdefault("tenant_ref", None)
    v.setdefault("instance_id", None)
    return v


def _mkjob(op: str, host_id: str, state="succeeded", detail="", polls=0) -> dict:
    jid = f"job-{next(_job_seq):05d}"
    j = {"job_id": jid, "op": op, "host_id": host_id, "state": state,
         "detail": detail, "remaining_polls": polls}
    _jobs[jid] = j
    return j


# ── hosts ──────────────────────────────────────────────────────────────
@router.get("/hosts")
def list_hosts(limit: int = 3000, offset: int = 0):
    """Full-fleet host list — one NicoHost per AI Infra compute tray (2,520),
    overlaid with any lifecycle state already touched via the bridge.

    Graceful: if AI Infra is unreachable, returns just the NICo-side overlay
    (whatever hosts have already been touched) rather than failing."""
    try:
        dpus = aiinfra.list_dpus(limit=limit, offset=offset)
    except aiinfra.AIInfraError:
        with STORE.lock:
            return [_view(h) for h in list(_hosts.values())[offset:offset + limit]]
    out = []
    with STORE.lock:
        for d in dpus:
            tid = d.get("compute_tray_id") or d.get("dpu_id", "").rsplit("-dpu", 1)[0]
            hid = f"nh-{tid}"
            h = _hosts.get(hid)
            if h is None:
                rec = _default_host(hid)
                # reflect live DPU health/tenant from AI Infra when untouched
                if d.get("tenants"):
                    rec["tenant_ref"] = d["tenants"][0]
                out.append(rec)
            else:
                out.append(_view(h))
        return out


@router.get("/hosts/{host_id}")
def get_host(host_id: str):
    with STORE.lock:
        return _view(_host(host_id))


@router.post("/hosts/{host_id}/reserve")
def reserve(host_id: str):
    with STORE.lock:
        h = _host(host_id); h["state"] = "reserved"
        STORE.event("info", "NeoCloudEmulator.1.0.HostReserved", [host_id])
        return _view(h)


@router.post("/hosts/{host_id}/unreserve")
def unreserve(host_id: str):
    with STORE.lock:
        h = _host(host_id); h["state"] = "pool_ready"
        return _view(h)


@router.post("/hosts/{host_id}/provision")
def provision(host_id: str, body: _ProvisionBody):
    """Provision a host: PXE-boot the backing tray and start the AI Infra
    provisioning workflow (DHCP lease + boot progression)."""
    with STORE.lock:
        h = _host(host_id); h["state"] = "provisioning"
        tid = _tray_id(host_id)
        detail = f"image={body.image_ref}"
        try:                            # drive the real twin on AI Infra
            aiinfra.reset_power(tid, "ForceRestart")
            prov = aiinfra.provision(tid, planned=True)
            detail += f" · {prov.get('lifecycle_state', 'Provisioning')}" \
                      f"/{prov.get('boot_stage', '')}"
        except aiinfra.AIInfraError as e:
            STORE.event("warning", "NeoCloudEmulator.1.0.BridgeProvisionSkipped",
                        [host_id, str(e)])
        STORE.event("info", "NeoCloudEmulator.1.0.HostProvisionStarted",
                    [host_id, body.image_ref])
        h["state"] = "provisioned"
        return _mkjob("provision", host_id, "succeeded", detail)


@router.post("/hosts/{host_id}/abort-provision")
def abort(host_id: str):
    with STORE.lock:
        h = _host(host_id); h["state"] = "pool_ready"
        return _view(h)


# ── instances (allocation = tenant DPU isolation) ──────────────────────
@router.post("/instances")
def allocate(body: _InstanceBody):
    """Allocate a host to a tenant. The isolating effect is a DPU tenant
    attachment created on AI Infra (VF + default-deny security policy)."""
    with STORE.lock:
        h = _host(body.host_id)
        iid = f"inst-{next(_job_seq):05d}"
        h["instance_id"] = iid; h["state"] = "allocated"
        h["tenant_id"] = body.tenant_ref
        did = _dpu_id(body.host_id)
        try:                            # drive DPU isolation on AI Infra
            att = aiinfra.attach_dpu(did, body.tenant_ref,
                                     _tenant_net(body.tenant_ref, did))
            h["_dpu"] = did
            h["_att_id"] = att.get("attachment_id")
        except aiinfra.AIInfraError as e:   # isolation is best-effort in the bridge
            STORE.event("warning", "NeoCloudEmulator.1.0.BridgeIsolationSkipped",
                        [body.host_id, str(e)])
        STORE.event("info", "NeoCloudEmulator.1.0.InstanceAllocated",
                    [body.host_id, body.tenant_ref, iid])
        return _view(h)


@router.delete("/instances/{instance_id}")
def release(instance_id: str):
    with STORE.lock:
        for h in _hosts.values():
            if h.get("instance_id") == instance_id:
                h["instance_id"] = None; h["state"] = "released"
                tenant = h.pop("tenant_id", None)
                did = h.pop("_dpu", None)
                att_id = h.pop("_att_id", None)
                if did and att_id:      # tear down DPU isolation on AI Infra
                    try:
                        aiinfra.detach_dpu(did, att_id)
                    except aiinfra.AIInfraError as e:
                        STORE.event("warning",
                                    "NeoCloudEmulator.1.0.BridgeDetachSkipped",
                                    [h["host_id"], str(e)])
                if tenant:
                    STORE.event("info",
                                "NeoCloudEmulator.1.0.TenantNetworkReleased",
                                [tenant])
                return _mkjob("release", h["host_id"], "succeeded")
        raise HTTPException(404, f"instance {instance_id} not found")


# ── sanitize / cordon ──────────────────────────────────────────────────
@router.post("/hosts/{host_id}/sanitize")
def sanitize(host_id: str):
    with STORE.lock:
        h = _host(host_id); h["state"] = "pool_ready"
        return _mkjob("sanitize", host_id, "succeeded",
                      detail="+".join(SANITIZE_STEPS))


@router.get("/hosts/{host_id}/sanitize-report")
def sanitize_report(host_id: str):
    with STORE.lock:
        _host(host_id)
        # shape matches nocp SanitizeReport: {host_id, passed, steps:[{step,ok}]}
        return {"host_id": host_id, "passed": True,
                "steps": [{"step": s, "ok": True} for s in SANITIZE_STEPS],
                "certificate_id": f"SAN-{abs(hash(host_id)) % 9000 + 1000}",
                "issued_at": _iso()}


@router.post("/hosts/{host_id}/cordon")
def cordon(host_id: str, body: _CordonBody):
    with STORE.lock:
        h = _host(host_id); h["cordoned"] = True
        STORE.event("warning", "NeoCloudEmulator.1.0.HostCordoned",
                    [host_id, body.reason])
        return _view(h)


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    j = _jobs.get(job_id)
    if not j:
        raise HTTPException(404, f"job {job_id} not found")
    return j


# ── SDN segments (tenant VPC / L3 EVPN) — the isolating stage ──────────
class _SegmentBody(BaseModel):
    tenant_ref: str
    vrf: str
    l3vni: int
    converged_vni: int
    host_ids: list = []
    allocation_id: Optional[str] = None


def _seg_view(s: dict) -> dict:
    return {k: v for k, v in s.items() if not k.startswith("_")}


@router.post("/segments")
def create_segment(body: _SegmentBody):
    """Create a tenant VPC segment and drive DPU isolation on each host's DPU
    (FNN L3 EVPN — vrf_dataplane vpc_<l3vni>) via AI Infra tenant-attachments.
    Same shape as nocp NicoSegment."""
    with STORE.lock:
        sid = STORE.nid("seg")
        attached = 0
        att_refs = []                   # [(dpu_id, att_id)] for teardown
        net_base = {"tenant_id": body.tenant_ref, "network_type": "vxlan",
                    "vni": body.l3vni, "vrf": body.vrf, "subnet": "10.200.0.0/16"}
        for hid in body.host_ids:
            did = _dpu_id(hid)
            try:
                net = {**net_base, "network_id": f"net-{body.tenant_ref}-{did}"}
                att = aiinfra.attach_dpu(did, body.tenant_ref, net)
                att_refs.append((did, att.get("attachment_id")))
                attached += 1
            except aiinfra.AIInfraError:
                pass                    # best-effort per host
        seg = {"segment_id": sid, "tenant_ref": body.tenant_ref, "vrf": body.vrf,
               "l3vni": body.l3vni, "converged_vni": body.converged_vni,
               "virtualizer": "fnn", "vrf_dataplane": f"vpc_{body.l3vni}",
               "host_ids": list(body.host_ids), "_attached": attached,
               "_att_refs": att_refs}
        _segments[sid] = seg
        STORE.event("info", "NeoCloudEmulator.1.0.SegmentCreated",
                    [body.tenant_ref, body.vrf, str(attached)])
        return _seg_view(seg)


@router.get("/segments")
def list_segments():
    with STORE.lock:
        return [_seg_view(s) for s in _segments.values()]


class _AttachBody(BaseModel):
    host_ids: list = []
    purpose: str = "converged"


@router.patch("/segments/{segment_id}/hosts")
def attach_hosts(segment_id: str, body: _AttachBody):
    """Attach extra hosts to an existing tenant segment — NOCP's Managed K8s
    control-plane (CPU) nodes join the tenant VPC on the Converged Network.
    Same contract as nocp's FakeNico.attach_hosts; physical DPU attachment on
    AI Infra is best-effort (CPU pool nodes have no rack-twin counterpart)."""
    with STORE.lock:
        s = _segments.get(segment_id)
        if not s:
            raise HTTPException(404, f"segment {segment_id} not found")
        added = [h for h in body.host_ids if h not in s["host_ids"]]
        s["host_ids"].extend(added)
        for hid in added:
            did = _dpu_id(hid)
            try:
                net = {"tenant_id": s["tenant_ref"], "network_type": "vxlan",
                       "vni": s["converged_vni"], "vrf": s["vrf"],
                       "subnet": "10.250.0.0/16",
                       "network_id": f"net-{s['tenant_ref']}-{did}-cvg"}
                att = aiinfra.attach_dpu(did, s["tenant_ref"], net)
                s.setdefault("_att_refs", []).append(
                    (did, att.get("attachment_id")))
            except aiinfra.AIInfraError:
                pass                    # best-effort per host
        STORE.event("info", "NeoCloudEmulator.1.0.SegmentHostsAttached",
                    [s["tenant_ref"], body.purpose, str(len(added))])
        return _seg_view(s)


@router.delete("/segments/{segment_id}")
def delete_segment(segment_id: str):
    with STORE.lock:
        s = _segments.pop(segment_id, None)
        if not s:
            raise HTTPException(404, f"segment {segment_id} not found")
        for did, att_id in s.get("_att_refs", []):
            if att_id:
                try:
                    aiinfra.detach_dpu(did, att_id)
                except aiinfra.AIInfraError:
                    pass
        STORE.event("info", "NeoCloudEmulator.1.0.SegmentDeleted",
                    [s["tenant_ref"], s["vrf"]])
        return _seg_view(s)
