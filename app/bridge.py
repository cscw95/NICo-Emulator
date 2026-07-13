"""NeoCloud OS (VRCM) integration bridge.

Exposes the exact REST contract that vrcm's NicoHttpAdapter speaks
(/hosts, /instances, /jobs with NicoHost/NicoJob shapes), backed by the
VR NVL72 twin + DPU isolation engine. Point vrcm at this base_url:

    VRCM_NICO_URL=http://127.0.0.1:9000/nico-bridge  ./run.sh

Lenient: accepts any host_id vrcm sends (auto-registers on first touch and,
when the id maps onto a twin compute tray, drives the real Redfish/DPU state).
"""
import itertools
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from .store import STORE, _iso

router = APIRouter(prefix="/nico-bridge", tags=["vrcm-bridge"])

# NICo-compatible host/job registry (host_id -> record)
_hosts = {}
_jobs = {}
_job_seq = itertools.count(1)

SANITIZE_STEPS = ["nvme_secure_erase", "gpu_memory_wipe", "system_memory_wipe",
                  "tpm_reset", "re_attestation", "firmware_revalidation",
                  "network_state_clear"]


class _ProvisionBody(BaseModel):
    image_ref: str = ""


class _InstanceBody(BaseModel):
    host_id: str
    tenant_ref: str


class _CordonBody(BaseModel):
    reason: str = ""


def _tray_for(host_id: str):
    """Map a vrcm host_id onto a twin compute tray when possible."""
    if host_id in STORE.trays:
        return STORE.trays[host_id]
    # heuristic: reuse the twin's trays round-robin so isolation has a backing DPU
    keys = list(STORE.trays)
    return STORE.trays[keys[hash(host_id) % len(keys)]] if keys else None


def _host(host_id: str, create=True) -> dict:
    h = _hosts.get(host_id)
    if h is None and create:
        tray = _tray_for(host_id)
        h = {"host_id": host_id, "tray_id": host_id, "sku": "vr-nvl72",
             "site": "STT Gasan", "state": "pool_ready", "firmware_ok": True,
             "attested": True, "cordoned": False, "instance_id": None,
             "_dpu": tray.dpu_id if tray else None}
        _hosts[host_id] = h
    if h is None:
        raise HTTPException(404, f"host {host_id} not found")
    return h


def _view(h: dict) -> dict:
    return {k: v for k, v in h.items() if not k.startswith("_")}


def _mkjob(op: str, host_id: str, state="succeeded", detail="", polls=0) -> dict:
    jid = f"job-{next(_job_seq):05d}"
    j = {"job_id": jid, "op": op, "host_id": host_id, "state": state,
         "detail": detail, "remaining_polls": polls}
    _jobs[jid] = j
    return j


@router.get("/hosts")
def list_hosts():
    with STORE.lock:
        return [_view(h) for h in _hosts.values()]


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
    with STORE.lock:
        h = _host(host_id); h["state"] = "provisioning"
        tray = STORE.trays.get(host_id) or (
            STORE.trays.get(h["_dpu"].rsplit("-dpu", 1)[0]) if h.get("_dpu") else None)
        if tray:                       # drive the real twin: PXE boot the tray
            tray.boot_source = "Pxe"; tray.lifecycle_state = "Provisioning"
            STORE.set_power(tray, "ForceRestart")
        STORE.event("info", "NeoCloudEmulator.1.0.HostProvisionStarted",
                    [host_id, body.image_ref])
        # emulator converges immediately; report a completed job
        h["state"] = "provisioned"
        return _mkjob("provision", host_id, "succeeded",
                      f"image={body.image_ref}")


@router.post("/hosts/{host_id}/abort-provision")
def abort(host_id: str):
    with STORE.lock:
        h = _host(host_id); h["state"] = "pool_ready"
        return _view(h)


@router.post("/instances")
def allocate(body: _InstanceBody):
    with STORE.lock:
        h = _host(body.host_id)
        iid = f"inst-{next(_job_seq):05d}"
        h["instance_id"] = iid; h["state"] = "allocated"; h["tenant_id"] = body.tenant_ref
        # drive DPU isolation: attach the tenant on the backing DPU
        did = h.get("_dpu")
        if did and did in STORE.dpus:
            try:
                from . import dpu as dpu_mod
                from . import models as m
                dpu_mod.create_attachment(did, m.AttachmentCreate(
                    tenant_id=body.tenant_ref,
                    network=m.TenantNetwork(
                        network_id=f"net-{body.tenant_ref}",
                        tenant_id=body.tenant_ref, network_type="vxlan",
                        vni=10000 + (hash(body.tenant_ref) % 6000),
                        vrf=body.tenant_ref, subnet="10.200.0.0/16")))
            except Exception as e:      # isolation is best-effort in the bridge
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
                return _mkjob("release", h["host_id"], "succeeded")
        raise HTTPException(404, f"instance {instance_id} not found")


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
        # shape matches vrcm SanitizeReport: {host_id, passed, steps:[{step,ok}]}
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
