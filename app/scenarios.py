"""Fault scenario engine — design §12 (DPU isolation validation suite).

Five built-in scenarios that drive the *real* DPU isolation engine end-to-end.
NICo no longer owns the DPU twin: each scenario now exercises the AI Infra
Emulator (:9100) DPU API over REST (``app.aiinfra`` — tenant-attachments,
traffic, faults, recover, ipsec-tunnels) and reads back the telemetry counters
from the AI Infra DPU detail. Assertions use ``>=`` on cumulative counters so
repeated runs remain valid.
"""
import itertools
from typing import Optional, List, Dict, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import aiinfra

router = APIRouter(prefix="/emulator/v1/scenarios", tags=["scenarios"])

# Fresh DPU used when the caller does not pin one (tray-04 of rack su-1-rack-00).
DEFAULT_DPU = "su-1-rack-00-tray-04-dpu-0"

# Unique tenant/mac suffixes per run so repeated runs never collide on AI Infra.
_run_seq = itertools.count(1)


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
                       "provisioned VF MAC. Anti-spoof must drop the frame and "
                       "raise the spoof counter.",
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
        "description": "An Arm OS crash must fail closed: the DPU goes critical, "
                       "dpu_arm_up drops to 0, and the compute tray is degraded. "
                       "Recovery restores service.",
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
def _rack_of(dpu_id: str) -> str:
    """su-1-rack-00-tray-04-dpu-0 -> su-1-rack-00"""
    parts = dpu_id.split("-")
    return "-".join(parts[:4]) if len(parts) >= 4 else dpu_id


def _telemetry(dpu_id: str) -> Dict[str, int]:
    return aiinfra.get_dpu(dpu_id).get("telemetry", {}) or {}


def _net(tenant: str, suffix: str, vni: int, subnet: str) -> dict:
    return {"network_id": f"{tenant}-net-{suffix}", "tenant_id": tenant,
            "network_type": "vxlan", "vni": vni, "subnet": subnet}


def _attach(dpu_id: str, tenant: str, suffix: str, vni: int, subnet: str,
            mac: str, encryption: bool = False) -> dict:
    return aiinfra.attach_dpu(
        dpu_id, tenant, _net(tenant, suffix, vni, subnet),
        security={"default_action": "deny", "spoof_check": True,
                  "allowed_macs": [mac], "allowed_source_cidrs": [subnet]},
        encryption={"enabled": encryption})


# ── scenario runners: each returns (steps, assertions) ─────────────────
def _run_inter_tenant(dpu_id: str, tag: str) -> Tuple[List[dict], List[dict]]:
    steps: List[dict] = []
    a = _attach(dpu_id, f"tenant-a-{tag}", f"a{tag}", 10001, "10.10.0.0/24",
                "02:aa:00:00:00:01")
    steps.append({"id": "attach-tenant-a", "ok": bool(a["ready"]),
                  "detail": f"tenant-a VF {a['function_id']} @ gen {a['generation']}"})
    b = _attach(dpu_id, f"tenant-b-{tag}", f"b{tag}", 10002, "10.20.0.0/24",
                "02:bb:00:00:00:01")
    steps.append({"id": "attach-tenant-b", "ok": bool(b["ready"]),
                  "detail": f"tenant-b VF {b['function_id']} @ gen {b['generation']}"})

    before = _telemetry(dpu_id)
    n = 1000
    r = aiinfra.send_traffic(dpu_id, {
        "source_function": a["function_id"],
        "destination_function": b["function_id"], "packet_count": n})
    after = _telemetry(dpu_id)
    steps.append({"id": "cross-tenant-traffic",
                  "ok": r["reason"] == "INTER_TENANT_DENY",
                  "detail": f"sent {n}, dropped {r['dropped']}, "
                            f"forwarded {r['forwarded']}, reason {r['reason']}"})

    assertions = [
        {"expr": "cross-tenant flow 100% dropped (dropped==1000, forwarded==0)",
         "ok": r["dropped"] == n and r["forwarded"] == 0},
        {"expr": "drop reason == INTER_TENANT_DENY",
         "ok": r["reason"] == "INTER_TENANT_DENY"},
        {"expr": "dpu_intertenant_drops_total increased by >= 1000",
         "ok": after.get("dpu_intertenant_drops_total", 0)
               - before.get("dpu_intertenant_drops_total", 0) >= n},
        {"expr": "dpu_default_deny_drops_total increased by >= 1000",
         "ok": after.get("dpu_default_deny_drops_total", 0)
               - before.get("dpu_default_deny_drops_total", 0) >= n},
    ]
    return steps, assertions


def _run_mac_spoof(dpu_id: str, tag: str) -> Tuple[List[dict], List[dict]]:
    steps: List[dict] = []
    real_mac = "02:aa:00:00:00:0a"
    a = _attach(dpu_id, f"tenant-a-{tag}", f"spoof{tag}", 10003, "10.30.0.0/24",
                real_mac)
    fid = a["function_id"]
    steps.append({"id": "attach-tenant-a", "ok": bool(a["ready"]),
                  "detail": f"VF {fid} provisioned MAC {real_mac}"})

    before = _telemetry(dpu_id).get("dpu_spoof_drops_total", 0)
    n = 500
    r = aiinfra.send_traffic(dpu_id, {
        "source_function": fid, "source_mac": "02:de:ad:be:ef:00",
        "packet_count": n})
    after = _telemetry(dpu_id).get("dpu_spoof_drops_total", 0)
    steps.append({"id": "spoofed-source-mac",
                  "ok": r["reason"] == "SOURCE_MAC_SPOOF",
                  "detail": f"sent {n} with forged MAC, dropped {r['dropped']}, "
                            f"reason {r['reason']}"})

    assertions = [
        {"expr": "spoofed frame dropped (reason==SOURCE_MAC_SPOOF)",
         "ok": r["reason"] == "SOURCE_MAC_SPOOF" and r["dropped"] == n},
        {"expr": "dpu_spoof_drops_total incremented", "ok": after > before},
    ]
    return steps, assertions


def _run_policy_rollback(dpu_id: str, tag: str) -> Tuple[List[dict], List[dict]]:
    steps: List[dict] = []
    a = _attach(dpu_id, f"tenant-a-{tag}", f"pol{tag}", 10004, "10.40.0.0/24",
                "02:aa:00:00:00:04")
    d0 = aiinfra.get_dpu(dpu_id)
    lkg = d0.get("last_known_good_generation", 0)
    steps.append({"id": "commit-good-policy", "ok": bool(a["ready"]),
                  "detail": f"attachment committed, last-known-good gen = {lkg}"})

    before = _telemetry(dpu_id).get("dpu_policy_apply_failures_total", 0)
    aiinfra.inject_fault(dpu_id, "DPU_FLOW_PROGRAMMING_FAILURE")
    d1 = aiinfra.get_dpu(dpu_id)
    after = _telemetry(dpu_id).get("dpu_policy_apply_failures_total", 0)
    cur = d1.get("current_policy_generation", 0)
    lkg2 = d1.get("last_known_good_generation", 0)
    steps.append({"id": "flow-programming-failure", "ok": cur == lkg2,
                  "detail": f"rolled back to gen {cur} (lkg {lkg2})"})

    assertions = [
        {"expr": "current_policy_generation == last_known_good_generation",
         "ok": cur == lkg2},
        {"expr": "dpu_policy_apply_failures_total incremented",
         "ok": after > before},
    ]
    return steps, assertions


def _run_arm_os(dpu_id: str, tag: str) -> Tuple[List[dict], List[dict]]:
    steps: List[dict] = []
    a = _attach(dpu_id, f"tenant-a-{tag}", f"arm{tag}", 10005, "10.50.0.0/24",
                "02:aa:00:00:00:05")
    steps.append({"id": "attach-tenant-a", "ok": bool(a["ready"]),
                  "detail": f"tenant function {a['function_id']} active"})

    fault = aiinfra.inject_fault(dpu_id, "DPU_ARM_OS_CRASH")
    d = aiinfra.get_dpu(dpu_id)
    arm_up = d.get("telemetry", {}).get("dpu_arm_up", 1)
    dpu_critical = d.get("health") == "critical" or fault.get("health") == "critical"
    # compute tray degraded — read from rack detail on AI Infra
    tray_id = d.get("compute_tray_id", "")
    tray_degraded = False
    try:
        rack = aiinfra.get_rack(_rack_of(dpu_id))
        for t in rack.get("tray_detail", []):
            if t.get("tray_id") == tray_id:
                tray_degraded = t.get("lifecycle_state") == "Degraded"
                break
    except aiinfra.AIInfraError:
        pass
    steps.append({"id": "arm-os-crash",
                  "ok": arm_up == 0 and dpu_critical,
                  "detail": f"dpu_arm_up={arm_up}, health={d.get('health')}, "
                            f"tray_degraded={tray_degraded}"})

    rec = aiinfra.recover_dpu(dpu_id)
    d2 = aiinfra.get_dpu(dpu_id)
    recovered = (d2.get("telemetry", {}).get("dpu_arm_up", 0) == 1
                 and d2.get("arm_os_state") == "ready")
    steps.append({"id": "recover", "ok": recovered,
                  "detail": f"post-recovery arm_os_state={d2.get('arm_os_state')}, "
                            f"health={rec.get('health')}"})

    assertions = [
        {"expr": "dpu_arm_up == 0 at crash", "ok": arm_up == 0},
        {"expr": "DPU fails closed (health critical)", "ok": dpu_critical},
        {"expr": "recovery restored dpu_arm_up == 1 / arm_os ready",
         "ok": recovered},
    ]
    return steps, assertions


def _run_ipsec(dpu_id: str, tag: str) -> Tuple[List[dict], List[dict]]:
    steps: List[dict] = []
    sa = aiinfra.create_ipsec(dpu_id, {
        "tenant_id": f"tenant-a-{tag}", "direction": "out", "spi": 0x1001,
        "peer_address": "10.60.5.2", "local_address": "10.60.5.1"})
    steps.append({"id": "establish-sa", "ok": sa.get("state") == "active",
                  "detail": f"IPsec SA {sa.get('sa_id')} state {sa.get('state')}"})

    before = _telemetry(dpu_id).get("dpu_ipsec_auth_failures_total", 0)
    aiinfra.inject_fault(dpu_id, "IPSEC_REKEY_FAILURE")
    after = _telemetry(dpu_id).get("dpu_ipsec_auth_failures_total", 0)
    steps.append({"id": "rekey-failure", "ok": after > before,
                  "detail": f"ipsec auth failures {before} -> {after}"})

    assertions = [
        {"expr": "dpu_ipsec_auth_failures_total incremented", "ok": after > before},
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
    """Execute a scenario end-to-end against a target AI Infra DPU."""
    if name not in _SCENARIO_NAMES:
        raise HTTPException(404, f"unknown scenario '{name}'")
    dpu_id = (body.dpu_id if body and body.dpu_id else DEFAULT_DPU)
    tag = f"s{next(_run_seq):04d}"

    try:
        before = _telemetry(dpu_id)          # 404/unreachable surfaces here
        steps, assertions = _RUNNERS[name](dpu_id, tag)
        after = _telemetry(dpu_id)
    except aiinfra.AIInfraError as e:
        raise HTTPException(502, f"AI Infra unavailable: {e}")

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
