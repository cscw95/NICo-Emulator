"""Provisioning emulator — bare-metal DHCP / PXE / iPXE / DNS + boot machine.

Emulates the out-of-band provisioning network for Vera Rubin NVL72 compute
trays: DHCP lease allocation on the BMC/host subnet, PXE → iPXE chainload, and
the host boot lifecycle (Discovered → Provisioning → Ready). Read-mostly views
plus a small state machine keyed on ComputeTray.boot_stage / lifecycle_state in
the shared twin (STORE). STORE.dhcp_leases is owned here (keyed by tray_id)."""
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from .store import STORE, RACK_ID, _iso

router = APIRouter(prefix="/emulator/v1", tags=["provisioning"])

# ── Boot state machine (PXE → DHCP → iPXE → OS → Host Agent) ───────────
BOOT_SEQUENCE = [
    "PXE Selected",
    "DHCP Lease",
    "iPXE Script Fetch",
    "OS Boot",
    "Host Agent Ready",
]
READY_STAGE = "HostReady"        # terminal boot_stage stored on the tray
LEASE_SECONDS = 86400
DNS_DOMAIN = "nico.local"


def _tray(tray_id: str):
    t = STORE.trays.get(tray_id)
    if not t:
        raise HTTPException(404, f"compute tray {tray_id} not found")
    return t


def _subnet(bmc_ip: str):
    o = (bmc_ip.split(".") + ["0", "0", "0", "0"])[:4]
    return o, f"{o[0]}.{o[1]}.{o[2]}"


def _hostname(tray_id: str) -> str:
    return f"{tray_id}.{DNS_DOMAIN}"


def _make_lease(tray) -> dict:
    o, base = _subnet(tray.bmc_ip)
    host = int(o[3]) + 100
    if host > 254:
        host = 100
    return {
        "tray_id": tray.tray_id,
        "mac_address": "52:54:00:%02x:%02x:%02x" % (
            int(o[1]) & 0xff, int(o[2]) & 0xff, int(o[3]) & 0xff),
        "ip_address": f"{base}.{host}",
        "subnet": f"{base}.0/24",
        "gateway": f"{base}.1",
        "dhcp_server": f"{base}.2",
        "lease_s": LEASE_SECONDS,
        "hostname": _hostname(tray.tray_id),
        "boot_source": "Pxe",
        "state": "bound",
        "allocated_at": _iso(),
    }


def _next_stage(stage: str) -> Optional[str]:
    if stage in BOOT_SEQUENCE:
        i = BOOT_SEQUENCE.index(stage)
        if i + 1 < len(BOOT_SEQUENCE):
            return BOOT_SEQUENCE[i + 1]
    return None


def _status(tray) -> dict:
    return {
        "tray_id": tray.tray_id,
        "lifecycle_state": tray.lifecycle_state,
        "boot_source": tray.boot_source,
        "boot_stage": tray.boot_stage,
        "lease": STORE.dhcp_leases.get(tray.tray_id),
    }


# ── DHCP ──────────────────────────────────────────────────────────────
@router.get("/dhcp/leases")
def leases(tray_id: Optional[str] = None):
    """List DHCP leases; optional ?tray_id= filter."""
    with STORE.lock:
        vals = list(STORE.dhcp_leases.values())
        if tray_id:
            vals = [l for l in vals if l.get("tray_id") == tray_id]
        return vals


@router.delete("/dhcp/leases/{tray_id}")
def release_lease(tray_id: str):
    """Release (delete) a compute tray's DHCP lease."""
    with STORE.lock:
        lease = STORE.dhcp_leases.pop(tray_id, None)
        if not lease:
            raise HTTPException(404, f"no dhcp lease for {tray_id}")
        STORE.event("info", "NeoCloudEmulator.1.0.DhcpLeaseReleased", [tray_id])
        return {"released": tray_id, "lease": lease}


# ── Provisioning / boot state machine ─────────────────────────────────
@router.post("/provision/{tray_id}")
def provision(tray_id: str):
    """Start provisioning a compute tray: allocate a DHCP lease and enter the
    PXE boot state machine."""
    with STORE.lock:
        tray = _tray(tray_id)
        lease = _make_lease(tray)
        STORE.dhcp_leases[tray_id] = lease
        tray.boot_source = "Pxe"
        tray.boot_enabled = "Continuous"
        tray.boot_stage = BOOT_SEQUENCE[0]          # "PXE Selected"
        tray.lifecycle_state = "Provisioning"
        STORE.event("info", "NeoCloudEmulator.1.0.ProvisioningStarted",
                    [tray_id, lease["ip_address"]])
        return {
            "tray_id": tray_id,
            "lifecycle_state": tray.lifecycle_state,
            "boot_stage": tray.boot_stage,
            "next_step": _next_stage(tray.boot_stage),
            "lease": lease,
        }


@router.post("/provision/{tray_id}/step")
def provision_step(tray_id: str):
    """Advance the boot state machine one step toward Host Agent Ready."""
    with STORE.lock:
        tray = _tray(tray_id)
        stage = tray.boot_stage
        if stage in (READY_STAGE, "Host Agent Ready"):
            return {"tray_id": tray_id, "lifecycle_state": tray.lifecycle_state,
                    "boot_stage": READY_STAGE, "stage": "Host Agent Ready",
                    "complete": True, "next_step": None}
        if stage not in BOOT_SEQUENCE:
            raise HTTPException(
                409, f"provisioning not started for {tray_id}; "
                     f"POST /emulator/v1/provision/{tray_id} first")
        new_stage = BOOT_SEQUENCE[min(BOOT_SEQUENCE.index(stage) + 1,
                                      len(BOOT_SEQUENCE) - 1)]
        complete = new_stage == "Host Agent Ready"
        if complete:
            tray.lifecycle_state = "Ready"
            tray.boot_stage = READY_STAGE
        else:
            tray.boot_stage = new_stage
        STORE.event("info", "NeoCloudEmulator.1.0.BootStageAdvanced",
                    [tray_id, new_stage])
        return {
            "tray_id": tray_id,
            "lifecycle_state": tray.lifecycle_state,
            "boot_stage": tray.boot_stage,
            "stage": new_stage,
            "complete": complete,
            "next_step": _next_stage(new_stage),
        }


@router.get("/provision/{tray_id}")
def provision_status(tray_id: str):
    """Current provisioning status: lifecycle_state, boot_stage, lease."""
    with STORE.lock:
        return _status(_tray(tray_id))


# ── PXE / iPXE ────────────────────────────────────────────────────────
@router.get("/pxe/boot.ipxe", response_class=PlainTextResponse)
def ipxe_script(tray_id: str):
    """Synthetic iPXE chainload script (plain text) for a compute tray."""
    with STORE.lock:
        tray = _tray(tray_id)
        lease = STORE.dhcp_leases.get(tray_id)
        _o, base = _subnet(tray.bmc_ip)
        http = f"http://{base}.2:8080"
        ip = lease["ip_address"] if lease else tray.bmc_ip
    return (
        "#!ipxe\n"
        f"# NICo synthetic iPXE chainload for {tray_id} ({RACK_ID})\n"
        f"set base-url {http}/vr/{tray_id}\n"
        f"set hostname {tray_id}\n"
        f"echo Provisioning {tray_id} :: NeoCloud OS bare-metal boot\n"
        "kernel ${base-url}/vmlinuz "
        f"ip={ip} console=ttyS0 nico.tray={tray_id} nico.rack={RACK_ID} "
        "nico.stage=provision\n"
        "initrd ${base-url}/initrd.img\n"
        "boot\n"
    )


# ── DNS ───────────────────────────────────────────────────────────────
@router.get("/dns/records")
def dns_records():
    """Synthetic forward (A) records for compute trays: hostname → bmc_ip
    (plus the leased host IP where a lease is active)."""
    with STORE.lock:
        recs = []
        for t in STORE.trays.values():
            recs.append({"hostname": _hostname(t.tray_id), "type": "A",
                         "address": t.bmc_ip, "ttl": 3600})
            lease = STORE.dhcp_leases.get(t.tray_id)
            if lease:
                recs.append({"hostname": f"host-{t.tray_id}.{DNS_DOMAIN}",
                             "type": "A", "address": lease["ip_address"],
                             "ttl": 3600})
        return {"domain": DNS_DOMAIN, "count": len(recs), "records": recs}
