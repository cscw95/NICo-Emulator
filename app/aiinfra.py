"""AI Infra Emulator (:9100) REST client.

NICo is a site-local control plane; it no longer owns the physical twin.
The physical state (racks/trays/DPUs/attachments/provisioning/DHCP) lives in
the AI Infra Emulator. This module wraps the AI Infra physical API with a
thin, timeout-guarded httpx client. NICo's orchestration modules (bridge /
sites / scenarios) delegate every physical mutation/read through here.

Base URL: env AI_INFRA_URL (default http://127.0.0.1:9100).
"""
import os
from typing import Optional, List, Dict, Any

import httpx

AI_INFRA_URL = os.environ.get("AI_INFRA_URL", "http://127.0.0.1:9100").rstrip("/")
TIMEOUT = float(os.environ.get("AI_INFRA_TIMEOUT", "8.0"))


class AIInfraError(RuntimeError):
    """Raised when the AI Infra Emulator is unreachable or returns an error."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _request(method: str, path: str, **kw) -> Any:
    url = f"{AI_INFRA_URL}{path}"
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.request(method, url, **kw)
    except httpx.HTTPError as e:
        raise AIInfraError(f"AI Infra unreachable ({method} {path}): {e}") from e
    if r.status_code >= 400:
        raise AIInfraError(
            f"AI Infra error {r.status_code} ({method} {path}): {r.text[:200]}",
            status=r.status_code)
    if not r.content:
        return None
    try:
        return r.json()
    except ValueError:
        return r.text


# ── health ─────────────────────────────────────────────────────────────
def ping() -> Dict[str, Any]:
    """Return {reachable, url, [detail]}. Never raises."""
    try:
        data = _request("GET", "/healthz")
        return {"reachable": True, "url": AI_INFRA_URL,
                "compute_trays": data.get("compute_trays"),
                "dpus": data.get("dpus"), "rack": data.get("rack")}
    except AIInfraError as e:
        return {"reachable": False, "url": AI_INFRA_URL, "detail": str(e)}


# ── cluster / racks ────────────────────────────────────────────────────
def get_rack(rack_id: str) -> Dict[str, Any]:
    return _request("GET", f"/emulator/v1/cluster/racks/{rack_id}")


def list_racks(site: Optional[str] = None, su: Optional[str] = None,
               q: Optional[str] = None, offset: int = 0,
               limit: int = 500) -> Dict[str, Any]:
    params = {"offset": offset, "limit": limit}
    if site:
        params["site"] = site
    if su:
        params["su"] = su
    if q:
        params["q"] = q
    return _request("GET", "/emulator/v1/cluster/racks", params=params)


def obs_summary() -> Dict[str, Any]:
    return _request("GET", "/emulator/v1/obs/summary")


def reset_twin() -> Dict[str, Any]:
    """Reset the AI Infra physical twin to its pristine seed (all tenant
    attachments/networks cleared). Used by the cascading emulator reset."""
    return _request("POST", "/emulator/v1/reset")


# ── Redfish power ──────────────────────────────────────────────────────
def reset_power(tray_id: str, reset_type: str = "On") -> Dict[str, Any]:
    return _request(
        "POST", f"/redfish/v1/Systems/{tray_id}/Actions/ComputerSystem.Reset",
        json={"ResetType": reset_type})


# ── provisioning (PXE / DHCP) ──────────────────────────────────────────
def provision(tray_id: str, planned: bool = False) -> Dict[str, Any]:
    # planned=True: 오케스트레이션이 의도한 신규 개통 — 재프로비저닝 장애 아님
    return _request("POST", f"/emulator/v1/provision/{tray_id}",
                    params={"planned": str(planned).lower()})


def provision_step(tray_id: str) -> Dict[str, Any]:
    return _request("POST", f"/emulator/v1/provision/{tray_id}/step")


def get_provision(tray_id: str) -> Dict[str, Any]:
    return _request("GET", f"/emulator/v1/provision/{tray_id}")


def list_leases() -> List[dict]:
    return _request("GET", "/emulator/v1/dhcp/leases") or []


# ── DPU isolation ──────────────────────────────────────────────────────
def list_dpus(rack: Optional[str] = None, site: Optional[str] = None,
              tenant: Optional[str] = None, q: Optional[str] = None,
              offset: int = 0, limit: int = 3000) -> List[dict]:
    params: Dict[str, Any] = {"offset": offset, "limit": limit}
    if rack:
        params["rack"] = rack
    if site:
        params["site"] = site
    if tenant:
        params["tenant"] = tenant
    if q:
        params["q"] = q
    return _request("GET", "/emulator/v1/dpus", params=params) or []


def get_dpu(dpu_id: str) -> Dict[str, Any]:
    return _request("GET", f"/emulator/v1/dpus/{dpu_id}")


def attach_dpu(dpu_id: str, tenant_id: str, network: Dict[str, Any],
               security: Optional[Dict[str, Any]] = None,
               encryption: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Create a tenant VF attachment (tenant isolation) on a DPU.

    Returns {attachment_id, function_id, representor_id, generation, ready}.
    """
    body: Dict[str, Any] = {"tenant_id": tenant_id, "network": network}
    if security:
        body["security"] = security
    if encryption:
        body["encryption"] = encryption
    return _request(
        "POST", f"/emulator/v1/dpus/{dpu_id}/tenant-attachments", json=body)


def detach_dpu(dpu_id: str, att_id: str) -> Dict[str, Any]:
    return _request(
        "DELETE", f"/emulator/v1/dpus/{dpu_id}/tenant-attachments/{att_id}")


def send_traffic(dpu_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    return _request("POST", f"/emulator/v1/dpus/{dpu_id}/traffic", json=body)


def inject_fault(dpu_id: str, fault_type: str,
                 parameters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {"type": fault_type}
    if parameters:
        body["parameters"] = parameters
    return _request("POST", f"/emulator/v1/dpus/{dpu_id}/faults", json=body)


def recover_dpu(dpu_id: str) -> Dict[str, Any]:
    return _request("POST", f"/emulator/v1/dpus/{dpu_id}/recover")


def create_ipsec(dpu_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    return _request("POST", f"/emulator/v1/dpus/{dpu_id}/ipsec-tunnels", json=body)
