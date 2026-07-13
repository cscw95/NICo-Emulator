"""Fabric emulator — NVLink / InfiniBand / Ethernet views over the twin.

Derives the rack's switching fabric from the seeded NVL72 topology (18 compute
trays, 9 NVSwitch trays) and the live DPU tenant attachments
(STORE.tenant_networks / STORE.attachments) so the fabric view stays consistent
with tenant isolation state:
  - NVLink : intra-rack GPU scale-up domain (all compute + nvswitch trays).
  - InfiniBand : one P_Key partition per tenant network (vni), members = the
    tenant's attached DPU functions; standalone partitions via POST.
  - Ethernet : one VXLAN segment per tenant network.
Read-mostly except the standalone-partition create."""
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from .store import STORE, RACK_ID, NVLINK_SWITCH_TRAYS, GPU_PER_TRAY, _iso

router = APIRouter(prefix="/emulator/v1/fabric", tags=["fabric"])

PKEY_BASE = 0x8000                       # IB partition base (management pkey)
# Standalone P_Key partitions created via POST (not derived from a network).
_STANDALONE_PARTITIONS: dict = {}


# ── derivation helpers ────────────────────────────────────────────────
def _members_for_network(network_id: str):
    return sorted({a["function_id"] for a in STORE.attachments.values()
                   if a.get("network_id") == network_id and a.get("function_id")})


def _members_for_tenant(tenant_id: str):
    return sorted({a["function_id"] for a in STORE.attachments.values()
                   if a.get("tenant_id") == tenant_id and a.get("function_id")})


def _derived_partitions():
    """One IB partition per tenant network that carries a VNI."""
    parts = []
    nets = sorted((n for n in STORE.tenant_networks.values() if n.get("vni")),
                  key=lambda n: n["network_id"])
    for i, net in enumerate(nets):
        pkey = PKEY_BASE | (i + 1)
        members = _members_for_network(net["network_id"])
        parts.append({
            "partition_id": f"pkey-{net['network_id']}",
            "pkey": "0x%04x" % pkey,
            "pkey_int": pkey,
            "network_id": net["network_id"],
            "tenant_id": net.get("tenant_id"),
            "vni": net.get("vni"),
            "membership": "full",
            "member_functions": members,
            "member_count": len(members),
            "source": "tenant_network",
            "state": "Active",
        })
    return parts


def _standalone_partitions():
    parts = []
    for p in _STANDALONE_PARTITIONS.values():
        members = _members_for_tenant(p["tenant_id"])
        parts.append({**p, "member_functions": members,
                      "member_count": len(members)})
    return parts


def _ib_partitions():
    return _derived_partitions() + _standalone_partitions()


# ── summary ───────────────────────────────────────────────────────────
@router.get("/summary")
def summary():
    with STORE.lock:
        nets = list(STORE.tenant_networks.values())
        vni_nets = [n for n in nets if n.get("vni")]
        parts = _ib_partitions()
        return {
            "rack_id": RACK_ID,
            "nvlink_domains": 1,
            "nvlink_switch_trays": NVLINK_SWITCH_TRAYS,
            "nvlink_member_trays": len(STORE.trays),
            "ib_partitions": len(vni_nets),
            "ethernet_segments": len(nets),
            "pkeys_allocated": len(parts),
            "tenant_networks": len(nets),
            "attachments": len(STORE.attachments),
        }


# ── NVLink ────────────────────────────────────────────────────────────
@router.get("/nvlink")
def nvlink():
    """NVLink scale-up domain over the rack."""
    with STORE.lock:
        compute = sorted(STORE.trays.keys())
        nvswitch = [f"{RACK_ID}-nvsw-{i:02d}"
                    for i in range(1, NVLINK_SWITCH_TRAYS + 1)]
        return {
            "domain_id": f"{RACK_ID}-nvl-domain-0",
            "rack_id": RACK_ID,
            "topology": "NVL72",
            "state": "Ready",
            "gpu_count": len(compute) * GPU_PER_TRAY,
            "compute_trays": compute,
            "compute_tray_count": len(compute),
            "nvswitch_trays": nvswitch,
            "nvswitch_tray_count": len(nvswitch),
            "bandwidth_per_gpu_gbps": 1800,
        }


# ── InfiniBand P_Key partitions ───────────────────────────────────────
class IbPartitionCreate(BaseModel):
    tenant_id: str
    pkey: Optional[int] = None
    membership: str = "full"


@router.get("/ib/partitions")
def ib_partitions():
    """IB P_Key partitions — one per tenant network (vni) + standalone ones."""
    with STORE.lock:
        parts = _ib_partitions()
        return {"count": len(parts), "partitions": parts}


@router.post("/ib/partitions")
def create_ib_partition(body: IbPartitionCreate):
    """Create a standalone IB partition (not tied to a tenant network)."""
    with STORE.lock:
        pkey = body.pkey if body.pkey is not None else (
            PKEY_BASE | (0x1000 + len(_STANDALONE_PARTITIONS) + 1))
        pid = STORE.nid("pkey")
        rec = {
            "partition_id": pid,
            "pkey": "0x%04x" % pkey,
            "pkey_int": pkey,
            "network_id": None,
            "tenant_id": body.tenant_id,
            "vni": None,
            "membership": body.membership,
            "source": "standalone",
            "state": "Active",
            "created_at": _iso(),
        }
        _STANDALONE_PARTITIONS[pid] = rec
        STORE.event("info", "NeoCloudEmulator.1.0.IbPartitionCreated",
                    [body.tenant_id, rec["pkey"]])
        members = _members_for_tenant(body.tenant_id)
        return {**rec, "member_functions": members, "member_count": len(members)}


# ── Ethernet VXLAN segments ───────────────────────────────────────────
@router.get("/ethernet/segments")
def ethernet_segments():
    """One L2/L3 segment per tenant network (VXLAN VNI / VLAN + VRF)."""
    with STORE.lock:
        segs = []
        for net in sorted(STORE.tenant_networks.values(),
                          key=lambda n: n["network_id"]):
            members = _members_for_network(net["network_id"])
            segs.append({
                "segment_id": f"seg-{net['network_id']}",
                "network_id": net["network_id"],
                "tenant_id": net.get("tenant_id"),
                "network_type": net.get("network_type"),
                "vni": net.get("vni"),
                "vlan_id": net.get("vlan_id"),
                "vrf": net.get("vrf"),
                "subnet": net.get("subnet"),
                "member_functions": members,
                "member_count": len(members),
                "state": "Up",
            })
        return {"count": len(segs), "segments": segs}


# ── switch inventory ──────────────────────────────────────────────────
@router.get("/switches")
def switches():
    """Leaf/spine (Ethernet) + NVSwitch (NVLink) switch inventory for the rack."""
    with STORE.lock:
        sw = []
        for i in range(1, NVLINK_SWITCH_TRAYS + 1):
            sw.append({"switch_id": f"{RACK_ID}-nvsw-{i:02d}", "role": "nvswitch",
                       "fabric": "nvlink", "ports": 72,
                       "state": "Ready", "link_state": "up"})
        for i in (1, 2):
            sw.append({"switch_id": f"{RACK_ID}-leaf-{i:02d}", "role": "leaf",
                       "fabric": "ethernet", "ports": 64,
                       "uplinks": [f"{RACK_ID}-spine-01"],
                       "state": "Ready", "link_state": "up"})
        sw.append({"switch_id": f"{RACK_ID}-spine-01", "role": "spine",
                   "fabric": "ethernet", "ports": 64,
                   "downlinks": [f"{RACK_ID}-leaf-01", f"{RACK_ID}-leaf-02"],
                   "state": "Ready", "link_state": "up"})
        return {
            "rack_id": RACK_ID,
            "count": len(sw),
            "roles": {"nvswitch": NVLINK_SWITCH_TRAYS, "leaf": 2, "spine": 1},
            "switches": sw,
        }
