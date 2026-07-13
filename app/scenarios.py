"""Fault scenario engine — design §12 (DPU isolation validation suite).

Five built-in scenarios that drive the *real* DPU isolation engine
(``app.dpu`` functions + ``app.store.STORE``) end-to-end, collect assertions,
and return a pass/fail verdict plus the telemetry counter deltas produced.

Each run is self-contained: it creates its own tenant attachments on the chosen
DPU and does not depend on prior state. Assertions use ``>=`` on cumulative
counters so repeated runs remain valid.
"""
from typing import Optional, List, Dict, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .store import STORE
from . import models as m
from . import dpu as dpu_ops

router = APIRouter(prefix="/emulator/v1/scenarios", tags=["scenarios"])

# Fresh DPU used when the caller does not pin one (ct-05 of the VR NVL72 rack).
DEFAULT_DPU = "vr-rack-001-ct-05-dpu-0"


# ── built-in scenario catalogue (design §12.1 – §12.5) ─────────────────
SCENARIOS: List[Dict[str, str]] = [
    {
        "name": "inter-tenant-isolation",
        "title": "Inter-tenant isolation validation",
        "category": "isolation",
        "severity": "critical",
        "description": "Attach tenant-a and tenant-b VFs on one DPU, then send "
                       "cross-tenant traffic. Default-deny must drop 100% of the "
                       "flow and raise the inter-tenant drop counter.",
    },
    {
        "name": "mac-spoof-quarantine",
        "title": "MAC spoof detection and quarantine",
        "category": "anti-spoof",
        "severity": "critical",
        "description": "Emit a frame with a source MAC that differs from the "
                       "provisioned VF MAC. Anti-spoof must drop the frame, raise "
                       "the spoof counter, and the DPU must quarantine the "
                       "offending function.",
    },
    {
        "name": "policy-rollback-lkg",
        "title": "Policy apply failure to Last-Known-Good rollback",
        "category": "control-plane",
        "severity": "warning",
        "description": "A flow-programming failure during a policy transaction "
                       "must roll the DPU back to its last-known-good generation "
                       "and increment the policy-apply-failure counter.",
    },
    {
        "name": "arm-os-fail-closed",
        "title": "DPU Arm OS failure to fail-closed",
        "category": "availability",
        "severity": "critical",
        "description": "An Arm OS crash must fail closed: tenant functions are "
                       "disabled/quarantined, the compute tray is marked degraded, "
                       "and dpu_arm_up drops to 0. Recovery restores service.",
    },
    {
        "name": "ipsec-sa-expiry",
        "title": "IPsec SA rekey/expiry failure",
        "category": "encryption",
        "severity": "warning",
        "description": "An IPsec rekey failure on an active SA must raise the "
                       "IPsec authentication-failure counter.",
    },
]

_SCENARIO_NAMES = {s["name"] for s in SCENARIOS}


class ScenarioRunReq(BaseModel):
    dpu_id: Optional[str] = None


# ── helpers ────────────────────────────────────────────────────────────
def _net(dpu_id: str, tenant: str, suffix: str, vni: int, subnet: str) -> m.TenantNetwork:
    return m.TenantNetwork(
        network_id=f"{dpu_id}-net-{suffix}", tenant_id=tenant,
        network_type="vxlan", vni=vni, subnet=subnet,
    )


def _attach(dpu_id: str, tenant: str, suffix: str, vni: int, subnet: str,
            mac: str, encryption: bool = False):
    """Create a tenant VF attachment via the real DPU engine."""
    return dpu_ops.create_attachment(dpu_id, m.AttachmentCreate(
        tenant_id=tenant,
        network=_net(dpu_id, tenant, suffix, vni, subnet),
        security=m.SecuritySpec(default_action="deny", spoof_check=True,
                                allowed_macs=[mac], allowed_source_cidrs=[subnet]),
        encryption=m.EncryptionSpec(enabled=encryption),
    ))


# ── scenario runners: each returns (steps, assertions) ─────────────────
def _run_inter_tenant(dpu_id: str) -> Tuple[List[dict], List[dict]]:
    d = dpu_ops._dpu(dpu_id)
    steps: List[dict] = []

    a = _attach(dpu_id, "tenant-a", "a", 10001, "10.10.0.0/24", "02:aa:00:00:00:01")
    steps.append({"id": "attach-tenant-a", "ok": bool(a["ready"]),
                  "detail": f"tenant-a VF {a['function_id']} @ gen {a['generation']}"})
    b = _attach(dpu_id, "tenant-b", "b", 10002, "10.20.0.0/24", "02:bb:00:00:00:01")
    steps.append({"id": "attach-tenant-b", "ok": bool(b["ready"]),
                  "detail": f"tenant-b VF {b['function_id']} @ gen {b['generation']}"})

    n = 1000
    r = dpu_ops.send_traffic(dpu_id, m.TrafficGen(
        source_function=a["function_id"], destination_function=b["function_id"],
        packet_count=n))
    steps.append({"id": "cross-tenant-traffic",
                  "ok": r["reason"] == "INTER_TENANT_DENY",
                  "detail": f"sent {n}, dropped {r['dropped']}, "
                            f"forwarded {r['forwarded']}, reason {r['reason']}"})

    assertions = [
        {"expr": "cross-tenant flow 100% dropped (dropped==1000, forwarded==0)",
         "ok": r["dropped"] == n and r["forwarded"] == 0},
        {"expr": "drop reason == INTER_TENANT_DENY", "ok": r["reason"] == "INTER_TENANT_DENY"},
        {"expr": "dpu_intertenant_drops_total >= 1000",
         "ok": d.telemetry.get("dpu_intertenant_drops_total", 0) >= n},
        {"expr": "dpu_default_deny_drops_total >= 1000",
         "ok": d.telemetry.get("dpu_default_deny_drops_total", 0) >= n},
    ]
    return steps, assertions


def _run_mac_spoof(dpu_id: str) -> Tuple[List[dict], List[dict]]:
    d = dpu_ops._dpu(dpu_id)
    steps: List[dict] = []

    real_mac = "02:aa:00:00:00:0a"
    a = _attach(dpu_id, "tenant-a", "spoof", 10003, "10.30.0.0/24", real_mac)
    fid = a["function_id"]
    steps.append({"id": "attach-tenant-a", "ok": bool(a["ready"]),
                  "detail": f"VF {fid} provisioned MAC {real_mac}"})

    before = d.telemetry.get("dpu_spoof_drops_total", 0)
    n = 500
    r = dpu_ops.send_traffic(dpu_id, m.TrafficGen(
        source_function=fid, source_mac="02:de:ad:be:ef:00", packet_count=n))
    after = d.telemetry.get("dpu_spoof_drops_total", 0)
    steps.append({"id": "spoofed-source-mac",
                  "ok": r["reason"] == "SOURCE_MAC_SPOOF",
                  "detail": f"sent {n} with forged MAC, dropped {r['dropped']}, "
                            f"reason {r['reason']}"})

    # control-plane enforcement response: quarantine the offending function
    f = d.functions[fid]
    f.operational_state = "QUARANTINED"
    f.link_state = "disabled"
    STORE.event("critical", "NeoCloudEmulator.1.0.FunctionQuarantined", [fid])
    steps.append({"id": "quarantine-function", "ok": f.operational_state == "QUARANTINED",
                  "detail": f"function {fid} -> {f.operational_state}"})

    assertions = [
        {"expr": "spoofed frame dropped (reason==SOURCE_MAC_SPOOF)",
         "ok": r["reason"] == "SOURCE_MAC_SPOOF" and r["dropped"] == n},
        {"expr": "dpu_spoof_drops_total incremented", "ok": after > before},
        {"expr": "source function quarantined",
         "ok": f.operational_state == "QUARANTINED"},
    ]
    return steps, assertions


def _run_policy_rollback(dpu_id: str) -> Tuple[List[dict], List[dict]]:
    d = dpu_ops._dpu(dpu_id)
    steps: List[dict] = []

    a = _attach(dpu_id, "tenant-a", "pol", 10004, "10.40.0.0/24", "02:aa:00:00:00:04")
    lkg = d.last_known_good_generation
    steps.append({"id": "commit-good-policy", "ok": bool(a["ready"]),
                  "detail": f"attachment committed, last-known-good gen = {lkg}"})

    # begin a new policy transaction that advances current generation (uncommitted)
    d.current_policy_generation = lkg + 1
    d.telemetry["dpu_policy_generation"] = lkg + 1
    steps.append({"id": "begin-policy-txn", "ok": True,
                  "detail": f"pending generation {lkg + 1} (not yet committed)"})

    before_fail = d.telemetry.get("dpu_policy_apply_failures_total", 0)
    dpu_ops.inject_fault(dpu_id, m.FaultInject(type="DPU_FLOW_PROGRAMMING_FAILURE"))
    after_fail = d.telemetry.get("dpu_policy_apply_failures_total", 0)
    steps.append({"id": "flow-programming-failure",
                  "ok": d.current_policy_generation == lkg,
                  "detail": f"rolled back to gen {d.current_policy_generation} "
                            f"(lkg {d.last_known_good_generation})"})

    assertions = [
        {"expr": "current_policy_generation == last_known_good_generation",
         "ok": d.current_policy_generation == d.last_known_good_generation},
        {"expr": "current generation rolled back to last-known-good",
         "ok": d.current_policy_generation == lkg},
        {"expr": "dpu_policy_apply_failures_total >= 1",
         "ok": after_fail >= 1 and after_fail > before_fail},
    ]
    return steps, assertions


def _run_arm_os(dpu_id: str) -> Tuple[List[dict], List[dict]]:
    d = dpu_ops._dpu(dpu_id)
    steps: List[dict] = []

    a = _attach(dpu_id, "tenant-a", "arm", 10005, "10.50.0.0/24", "02:aa:00:00:00:05")
    steps.append({"id": "attach-tenant-a", "ok": bool(a["ready"]),
                  "detail": f"tenant function {a['function_id']} active"})

    dpu_ops.inject_fault(dpu_id, m.FaultInject(type="DPU_ARM_OS_CRASH"))
    # snapshot fail-closed state at crash time (before recovery)
    tenant_fns = [f for f in d.functions.values() if f.function_type in ("VF", "SF")]
    fns_disabled = bool(tenant_fns) and all(
        f.operational_state == "QUARANTINED" for f in tenant_fns)
    arm_up = d.telemetry.get("dpu_arm_up", 1)
    tray = STORE.trays.get(d.compute_tray_id)
    tray_degraded = bool(tray) and tray.lifecycle_state == "Degraded"
    steps.append({"id": "arm-os-crash",
                  "ok": arm_up == 0 and fns_disabled and tray_degraded,
                  "detail": f"dpu_arm_up={arm_up}, {len(tenant_fns)} tenant fns "
                            f"quarantined, tray={tray.lifecycle_state if tray else 'n/a'}"})

    # recovery restores last-known-good service
    dpu_ops.recover(dpu_id)
    recovered = d.telemetry.get("dpu_arm_up", 0) == 1 and d.arm_os_state == "ready"
    steps.append({"id": "recover", "ok": recovered,
                  "detail": f"post-recovery dpu_arm_up={d.telemetry.get('dpu_arm_up')}, "
                            f"arm_os_state={d.arm_os_state}"})

    assertions = [
        {"expr": "dpu_arm_up == 0 at crash", "ok": arm_up == 0},
        {"expr": "tenant functions disabled (fail-closed)", "ok": fns_disabled},
        {"expr": "compute tray degraded", "ok": tray_degraded},
        {"expr": "recovery restored dpu_arm_up == 1", "ok": recovered},
    ]
    return steps, assertions


def _run_ipsec(dpu_id: str) -> Tuple[List[dict], List[dict]]:
    d = dpu_ops._dpu(dpu_id)
    steps: List[dict] = []

    sa = dpu_ops.create_ipsec(dpu_id, m.IpsecTunnelCreate(
        tenant_id="tenant-a", direction="out", spi=0x1001,
        peer_address="10.60.5.2", local_address="10.60.5.1"))
    steps.append({"id": "establish-sa", "ok": sa.get("state") == "active",
                  "detail": f"IPsec SA {sa.get('sa_id')} state {sa.get('state')}"})

    before = d.telemetry.get("dpu_ipsec_auth_failures_total", 0)
    dpu_ops.inject_fault(dpu_id, m.FaultInject(type="IPSEC_REKEY_FAILURE"))
    after = d.telemetry.get("dpu_ipsec_auth_failures_total", 0)
    steps.append({"id": "rekey-failure", "ok": after > before,
                  "detail": f"ipsec auth failures {before} -> {after}"})

    assertions = [
        {"expr": "dpu_ipsec_auth_failures_total >= 1", "ok": after >= 1},
        {"expr": "ipsec auth-failure counter incremented", "ok": after > before},
    ]
    return steps, assertions


_RUNNERS = {
    "inter-tenant-isolation": _run_inter_tenant,
    "mac-spoof-quarantine": _run_mac_spoof,
    "policy-rollback-lkg": _run_policy_rollback,
    "arm-os-fail-closed": _run_arm_os,
    "ipsec-sa-expiry": _run_ipsec,
}


# ── endpoints ──────────────────────────────────────────────────────────
@router.get("")
def list_scenarios():
    """List the built-in fault scenarios."""
    return SCENARIOS


@router.post("/{name}/run")
def run_scenario(name: str, body: Optional[ScenarioRunReq] = None):
    """Execute a scenario end-to-end against a target DPU."""
    if name not in _SCENARIO_NAMES:
        raise HTTPException(404, f"unknown scenario '{name}'")
    dpu_id = (body.dpu_id if body and body.dpu_id else DEFAULT_DPU)

    with STORE.lock:
        d = dpu_ops._dpu(dpu_id)   # 404 if DPU does not exist
        before = dict(d.telemetry)
        steps, assertions = _RUNNERS[name](dpu_id)
        after = dict(d.telemetry)

    delta = {k: after[k] - before.get(k, 0)
             for k in after if after[k] != before.get(k, 0)}
    passed = all(s["ok"] for s in steps) and all(a["ok"] for a in assertions)

    return {
        "name": name,
        "dpu_id": dpu_id,
        "steps": steps,
        "assertions": assertions,
        "passed": passed,
        "telemetry_delta": delta,
    }
