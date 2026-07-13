"""In-memory state store + Vera Rubin NVL72 digital twin.

Thread-safe (RLock). Mirrors the design's DB schema as dataclasses:
dpu / dpu_function / dpu_representor / tenant_network / dpu_tenant_attachment /
dpu_security_policy / dpu_flow_pipe / dpu_flow_entry / dpu_qos_policy /
dpu_ipsec_sa / dpu_policy_transaction  (+ Redfish BMC + fabric state)."""
import itertools
import time
from collections import deque
from dataclasses import dataclass, field
from threading import RLock
from typing import Dict, List, Optional, Any

# ── VR NVL72 constants (NCP RD) ───────────────────────────────────────
RACK_ID = "vr-rack-001"
COMPUTE_TRAYS = 18          # NVL72: 18 compute trays × 4 Rubin GPU = 72 GPU
GPU_PER_TRAY = 4
NVLINK_SWITCH_TRAYS = 9
POWER_SHELVES = 6
CDU_COUNT = 1


def _now() -> float:
    return time.monotonic()


def _iso(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── DPU domain records ────────────────────────────────────────────────
@dataclass
class Function:
    function_id: str
    dpu_id: str
    function_type: str          # PF | VF | SF
    pf_number: Optional[int] = None
    vf_number: Optional[int] = None
    sf_number: Optional[int] = None
    controller_number: Optional[int] = None
    pci_address: Optional[str] = None
    mac_address: Optional[str] = None
    tenant_id: Optional[str] = None
    trusted: bool = False
    spoof_check: bool = True
    link_state: str = "auto"
    operational_state: str = "INACTIVE"   # design §6.3 state machine
    rx_packets: int = 0
    tx_packets: int = 0
    tx_forwarded_packets: int = 0


@dataclass
class Representor:
    representor_id: str
    dpu_id: str
    function_id: str
    netdev_name: str
    switch_id: str
    port_index: int
    admin_state: str = "up"
    operational_state: str = "up"


@dataclass
class Dpu:
    dpu_id: str
    compute_tray_id: str
    bmc_ip: str
    operating_mode: str = "DPU"          # NIC | DPU | Restricted | ZeroTrust
    eswitch_mode: str = "switchdev"
    arm_os_state: str = "ready"
    bmc_state: str = "ok"
    secure_boot_enabled: bool = True
    attestation_state: str = "VALID"
    current_policy_generation: int = 0
    last_known_good_generation: int = 0
    health: str = "ok"
    failure_policy: Dict[str, str] = field(default_factory=lambda: {
        "datapath_controller_failure": "failClosed",
        "arm_os_failure": "failClosed",
        "control_plane_disconnect": "preserveLastKnownGood",
    })
    functions: Dict[str, Function] = field(default_factory=dict)
    representors: Dict[str, Representor] = field(default_factory=dict)
    telemetry: Dict[str, int] = field(default_factory=dict)  # counter name -> value

    def counter(self, name: str, inc: int = 0) -> int:
        self.telemetry[name] = self.telemetry.get(name, 0) + inc
        return self.telemetry[name]


@dataclass
class ComputeTray:
    tray_id: str
    bmc_ip: str
    gpus: int = GPU_PER_TRAY
    power_state: str = "On"              # Off | PoweringOn | On
    power_target: str = "On"
    power_changed_at: float = field(default_factory=_now)
    boot_source: str = "None"
    boot_enabled: str = "Disabled"
    boot_stage: str = "HostReady"        # boot progression state
    lifecycle_state: str = "Ready"       # Discovered|Provisioning|Ready|Degraded
    health: str = "ok"
    dpu_id: str = ""


# ── Store ─────────────────────────────────────────────────────────────
class Store:
    def __init__(self):
        self.lock = RLock()
        self.started = _iso()
        self.trays: Dict[str, ComputeTray] = {}
        self.dpus: Dict[str, Dpu] = {}
        self.tenant_networks: Dict[str, dict] = {}
        self.attachments: Dict[str, dict] = {}
        self.security_policies: Dict[str, dict] = {}
        self.flow_pipes: Dict[str, dict] = {}
        self.flow_entries: Dict[str, dict] = {}
        self.qos_policies: Dict[str, dict] = {}
        self.ipsec_sas: Dict[str, dict] = {}
        self.policy_txns: Dict[str, dict] = {}
        self.dhcp_leases: Dict[str, dict] = {}
        self.events = deque(maxlen=1000)
        self._ids = itertools.count(1)
        self.seed()

    def nid(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids):04d}"

    def event(self, severity: str, message_id: str, args=None, **extra):
        e = {"at": _iso(), "severity": severity, "message_id": message_id,
             "args": args or [], **extra}
        self.events.append(e)
        return e

    # ── VR NVL72 twin ─────────────────────────────────────────────────
    def seed(self):
        with self.lock:
            self.trays.clear(); self.dpus.clear()
            for i in range(1, COMPUTE_TRAYS + 1):
                tid = f"{RACK_ID}-ct-{i:02d}"
                did = f"{tid}-dpu-0"
                self.trays[tid] = ComputeTray(
                    tray_id=tid, bmc_ip=f"10.60.{i}.10", dpu_id=did)
                dpu = Dpu(dpu_id=did, compute_tray_id=tid,
                          bmc_ip=f"10.60.{i}.11")
                # each DPU exposes 2 host PFs (pf0/pf1)
                for pf in (0, 1):
                    fid = f"{did}-pf-{pf}"
                    dpu.functions[fid] = Function(
                        function_id=fid, dpu_id=did, function_type="PF",
                        pf_number=pf, pci_address=f"0000:03:00.{pf}",
                        operational_state="ACTIVE")
                _init_dpu_telemetry(dpu)
                self.dpus[did] = dpu
            self.event("info", "NeoCloudEmulator.1.0.TwinSeeded",
                       [RACK_ID, str(COMPUTE_TRAYS)])

    def reset(self):
        with self.lock:
            self.tenant_networks.clear(); self.attachments.clear()
            self.security_policies.clear(); self.flow_pipes.clear()
            self.flow_entries.clear(); self.qos_policies.clear()
            self.ipsec_sas.clear(); self.policy_txns.clear()
            self.dhcp_leases.clear(); self.events.clear()
            self.seed()

    # ── Redfish power state machine (lazy time-based) ─────────────────
    def power_state(self, tray: ComputeTray) -> str:
        if tray.power_target == "On" and tray.power_state != "On":
            if _now() - tray.power_changed_at > 1.0:
                tray.power_state = "On"
            else:
                tray.power_state = "PoweringOn"
        return tray.power_state

    def set_power(self, tray: ComputeTray, reset_type: str):
        tray.power_changed_at = _now()
        if reset_type in ("On", "ForceRestart", "GracefulRestart"):
            tray.power_target = "On"; tray.power_state = "PoweringOn"
            if reset_type == "ForceRestart":
                tray.boot_stage = "PXE Selected" if tray.boot_source == "Pxe" else "HostReady"
        elif reset_type in ("ForceOff", "GracefulShutdown"):
            tray.power_target = "Off"; tray.power_state = "Off"


def _init_dpu_telemetry(dpu: Dpu):
    for k in ("dpu_flow_entries", "dpu_flow_entry_packets_total",
              "dpu_flow_entry_drops_total", "dpu_acl_drops_total",
              "dpu_spoof_drops_total", "dpu_vlan_violation_drops_total",
              "dpu_vni_violation_drops_total", "dpu_intertenant_drops_total",
              "dpu_default_deny_drops_total", "dpu_policy_apply_failures_total",
              "dpu_ipsec_auth_failures_total"):
        dpu.telemetry[k] = 0
    dpu.telemetry["dpu_arm_up"] = 1
    dpu.telemetry["dpu_up"] = 1
    dpu.telemetry["dpu_policy_generation"] = 0
    dpu.telemetry["dpu_last_known_good_generation"] = 0


STORE = Store()
